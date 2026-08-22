use regex::RegexBuilder;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;
use std::fs;
use std::io::{Read, Write};
use std::path::{Component, Path, PathBuf};
use std::process::{Command, Stdio};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};
use tempfile::Builder as TempDirBuilder;
use wait_timeout::ChildExt;
use walkdir::{DirEntry, WalkDir};

pub mod database;
pub mod deployment;
pub mod local_agent;
pub mod ops;

const DEFAULT_MAX_OUTPUT_BYTES: usize = 256_000;
const MAX_READ_BYTES: usize = 512_000;
const MAX_FILES: usize = 10_000;
const MAX_SEARCH_MATCHES: usize = 200;

#[derive(Debug, Deserialize)]
#[serde(tag = "tool", rename_all = "snake_case", deny_unknown_fields)]
pub enum ToolRequest {
    Inventory {
        #[serde(default)]
        path: String,
        #[serde(default = "default_depth")]
        max_depth: usize,
    },
    SearchLiteral {
        query: String,
        #[serde(default)]
        paths: Vec<String>,
        #[serde(default)]
        case_sensitive: bool,
    },
    ReadLines {
        path: String,
        start_line: usize,
        end_line: usize,
    },
    HashFile {
        path: String,
    },
    ProjectSummary,
    FindSymbols {
        query: String,
        #[serde(default)]
        paths: Vec<String>,
    },
    ApplyExactPatch {
        path: String,
        expected_sha256: String,
        old: String,
        replacement: String,
    },
    CreateFile {
        path: String,
        expected_absent: bool,
        content: String,
    },
    GitStatus,
    GitDiff {
        #[serde(default)]
        staged: bool,
        #[serde(default)]
        path: Option<String>,
    },
    RunValidation {
        profile: ValidationProfile,
        #[serde(default = "default_timeout")]
        timeout_seconds: u64,
    },
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ValidationProfile {
    PythonCompile,
    PythonUnit,
    PythonLint,
    WebLint,
    WebBuild,
    FlutterAnalyze,
    FlutterTest,
    RustFormat,
    RustTest,
    GitDiffCheck,
}

#[derive(Debug, Serialize)]
pub struct ToolResponse {
    pub ok: bool,
    pub tool: String,
    pub duration_ms: u128,
    pub result: Value,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<ToolError>,
}

#[derive(Debug, Serialize)]
pub struct ToolError {
    pub code: String,
    pub message: String,
}

#[derive(Debug)]
pub struct BrokerError {
    code: &'static str,
    message: String,
}

impl BrokerError {
    fn new(code: &'static str, message: impl Into<String>) -> Self {
        Self {
            code,
            message: message.into(),
        }
    }
}

pub struct Broker {
    root: PathBuf,
    max_output_bytes: usize,
    allow_mutations: bool,
}

pub fn parse_request(input: &str) -> Result<ToolRequest, BrokerError> {
    let value: Value = serde_json::from_str(input)
        .map_err(|_| BrokerError::new("invalid_request", "request is not valid JSON"))?;
    let object = value
        .as_object()
        .ok_or_else(|| BrokerError::new("invalid_request", "request must be an object"))?;
    let tool = object
        .get("tool")
        .and_then(Value::as_str)
        .ok_or_else(|| BrokerError::new("invalid_request", "request must contain a tool"))?;
    let allowed: &[&str] = match tool {
        "inventory" => &["tool", "path", "max_depth"],
        "search_literal" => &["tool", "query", "paths", "case_sensitive"],
        "read_lines" => &["tool", "path", "start_line", "end_line"],
        "hash_file" => &["tool", "path"],
        "project_summary" => &["tool"],
        "find_symbols" => &["tool", "query", "paths"],
        "apply_exact_patch" => &["tool", "path", "expected_sha256", "old", "replacement"],
        "create_file" => &["tool", "path", "expected_absent", "content"],
        "git_status" => &["tool"],
        "git_diff" => &["tool", "staged", "path"],
        "run_validation" => &["tool", "profile", "timeout_seconds"],
        _ => return Err(BrokerError::new("unknown_tool", "tool is not registered")),
    };
    reject_unknown_fields(object, allowed)?;
    serde_json::from_value(value)
        .map_err(|_| BrokerError::new("invalid_request", "request violates the typed tool schema"))
}

fn reject_unknown_fields(object: &Map<String, Value>, allowed: &[&str]) -> Result<(), BrokerError> {
    if object.keys().any(|key| !allowed.contains(&key.as_str())) {
        return Err(BrokerError::new(
            "unknown_field",
            "request contains a field outside the typed tool schema",
        ));
    }
    Ok(())
}

impl Broker {
    pub fn new(root: impl AsRef<Path>) -> Result<Self, BrokerError> {
        let root = root.as_ref().canonicalize().map_err(|error| {
            BrokerError::new(
                "invalid_root",
                format!("cannot resolve workspace root: {error}"),
            )
        })?;
        if !root.is_dir() {
            return Err(BrokerError::new(
                "invalid_root",
                "workspace root is not a directory",
            ));
        }
        Ok(Self {
            root,
            max_output_bytes: DEFAULT_MAX_OUTPUT_BYTES,
            allow_mutations: false,
        })
    }

    /// Creates a broker that may apply hash-guarded structured patches.
    ///
    /// This constructor is intended only for an ephemeral workspace owned by a
    /// trusted orchestrator. The regular broker remains read-only.
    pub fn new_mutable(root: impl AsRef<Path>) -> Result<Self, BrokerError> {
        let mut broker = Self::new(root)?;
        broker.allow_mutations = true;
        Ok(broker)
    }

    pub fn execute(&self, request: ToolRequest) -> ToolResponse {
        let started = Instant::now();
        let tool = request.tool_name().to_string();
        let outcome = match request {
            ToolRequest::Inventory { path, max_depth } => self.inventory(&path, max_depth),
            ToolRequest::SearchLiteral {
                query,
                paths,
                case_sensitive,
            } => self.search_literal(&query, &paths, case_sensitive),
            ToolRequest::ReadLines {
                path,
                start_line,
                end_line,
            } => self.read_lines(&path, start_line, end_line),
            ToolRequest::HashFile { path } => self.hash_file(&path),
            ToolRequest::ProjectSummary => self.project_summary(),
            ToolRequest::FindSymbols { query, paths } => self.find_symbols(&query, &paths),
            ToolRequest::ApplyExactPatch {
                path,
                expected_sha256,
                old,
                replacement,
            } => self.apply_exact_patch(&path, &expected_sha256, &old, &replacement),
            ToolRequest::CreateFile {
                path,
                expected_absent,
                content,
            } => self.create_file(&path, expected_absent, &content),
            ToolRequest::GitStatus => {
                self.run_program("git", &["status", "--short", "--branch"], &self.root, 20)
            }
            ToolRequest::GitDiff { staged, path } => self.git_diff(staged, path.as_deref()),
            ToolRequest::RunValidation {
                profile,
                timeout_seconds,
            } => self.run_validation(profile, timeout_seconds),
        };
        match outcome {
            Ok(result) => ToolResponse {
                ok: true,
                tool,
                duration_ms: started.elapsed().as_millis(),
                result,
                error: None,
            },
            Err(error) => ToolResponse {
                ok: false,
                tool,
                duration_ms: started.elapsed().as_millis(),
                result: json!({}),
                error: Some(ToolError {
                    code: error.code.to_string(),
                    message: error.message,
                }),
            },
        }
    }

    fn inventory(&self, path: &str, max_depth: usize) -> Result<Value, BrokerError> {
        if max_depth == 0 || max_depth > 20 {
            return Err(BrokerError::new(
                "invalid_argument",
                "max_depth must be between 1 and 20",
            ));
        }
        let start = self.safe_path(path, true, false)?;
        let mut files = Vec::new();
        let mut truncated = false;
        for entry in WalkDir::new(&start)
            .max_depth(max_depth)
            .follow_links(false)
            .into_iter()
            .filter_entry(is_visible_entry)
        {
            let entry =
                entry.map_err(|error| BrokerError::new("walk_failed", error.to_string()))?;
            if entry.file_type().is_symlink() || !entry.file_type().is_file() {
                continue;
            }
            let relative = entry
                .path()
                .strip_prefix(&self.root)
                .map_err(|_| BrokerError::new("path_escape", "inventory path escaped workspace"))?;
            if path_is_sensitive(relative) || path_is_generated(relative) {
                continue;
            }
            files.push(relative.to_string_lossy().replace('\\', "/"));
            if files.len() >= MAX_FILES {
                truncated = true;
                break;
            }
        }
        files.sort();
        Ok(json!({"files": files, "truncated": truncated}))
    }

    fn search_literal(
        &self,
        query: &str,
        paths: &[String],
        case_sensitive: bool,
    ) -> Result<Value, BrokerError> {
        if query.trim().is_empty() || query.len() > 1_000 {
            return Err(BrokerError::new(
                "invalid_argument",
                "query must contain 1 to 1000 characters",
            ));
        }
        let matcher = RegexBuilder::new(&regex::escape(query))
            .case_insensitive(!case_sensitive)
            .build()
            .map_err(|error| BrokerError::new("invalid_argument", error.to_string()))?;
        let roots = if paths.is_empty() {
            vec![self.root.clone()]
        } else {
            paths
                .iter()
                .map(|path| self.safe_path(path, true, false))
                .collect::<Result<Vec<_>, _>>()?
        };
        let mut matches = Vec::new();
        let mut seen = BTreeSet::new();
        let mut truncated = false;
        for root in roots {
            let entries: Box<dyn Iterator<Item = Result<DirEntry, walkdir::Error>>> =
                if root.is_file() {
                    Box::new(WalkDir::new(root).max_depth(0).into_iter())
                } else {
                    Box::new(
                        WalkDir::new(root)
                            .follow_links(false)
                            .into_iter()
                            .filter_entry(is_visible_entry),
                    )
                };
            for entry in entries {
                let entry =
                    entry.map_err(|error| BrokerError::new("walk_failed", error.to_string()))?;
                if !entry.file_type().is_file() || entry.file_type().is_symlink() {
                    continue;
                }
                let relative = entry.path().strip_prefix(&self.root).map_err(|_| {
                    BrokerError::new("path_escape", "search path escaped workspace")
                })?;
                if path_is_sensitive(relative)
                    || path_is_generated(relative)
                    || !seen.insert(relative.to_path_buf())
                {
                    continue;
                }
                let metadata = entry
                    .metadata()
                    .map_err(|error| BrokerError::new("read_failed", error.to_string()))?;
                if metadata.len() as usize > MAX_READ_BYTES || !is_text_candidate(relative) {
                    continue;
                }
                let content = fs::read_to_string(entry.path()).unwrap_or_default();
                for (index, line) in content.lines().enumerate() {
                    if matcher.is_match(line) {
                        matches.push(json!({
                            "path": relative.to_string_lossy().replace('\\', "/"),
                            "line": index + 1,
                            "excerpt": bounded_chars(line.trim(), 500),
                        }));
                        if matches.len() >= MAX_SEARCH_MATCHES {
                            truncated = true;
                            break;
                        }
                    }
                }
                if truncated {
                    break;
                }
            }
            if truncated {
                break;
            }
        }
        Ok(json!({"matches": matches, "truncated": truncated}))
    }

    fn read_lines(
        &self,
        path: &str,
        start_line: usize,
        end_line: usize,
    ) -> Result<Value, BrokerError> {
        if start_line == 0 || end_line < start_line || end_line - start_line > 2_000 {
            return Err(BrokerError::new(
                "invalid_argument",
                "line range is invalid or exceeds 2001 lines",
            ));
        }
        let path = self.safe_path(path, true, true)?;
        let metadata = fs::metadata(&path)
            .map_err(|error| BrokerError::new("read_failed", error.to_string()))?;
        if metadata.len() as usize > MAX_READ_BYTES {
            return Err(BrokerError::new(
                "read_limit",
                "file exceeds the bounded read limit",
            ));
        }
        let content = fs::read_to_string(&path)
            .map_err(|error| BrokerError::new("read_failed", error.to_string()))?;
        let selected = content
            .lines()
            .enumerate()
            .filter(|(index, _)| *index + 1 >= start_line && *index < end_line)
            .map(|(_, line)| line)
            .collect::<Vec<_>>()
            .join("\n");
        Ok(
            json!({"path": self.relative(&path)?, "start_line": start_line, "end_line": end_line, "content": selected}),
        )
    }

    fn hash_file(&self, path: &str) -> Result<Value, BrokerError> {
        let path = self.safe_path(path, true, true)?;
        let mut file = fs::File::open(&path)
            .map_err(|error| BrokerError::new("read_failed", error.to_string()))?;
        let mut hasher = Sha256::new();
        let bytes = std::io::copy(&mut file, &mut hasher)
            .map_err(|error| BrokerError::new("read_failed", error.to_string()))?;
        Ok(
            json!({"path": self.relative(&path)?, "bytes": bytes, "sha256": format!("{:x}", hasher.finalize())}),
        )
    }

    fn project_summary(&self) -> Result<Value, BrokerError> {
        let mut extensions = std::collections::BTreeMap::<String, usize>::new();
        let mut manifests = Vec::new();
        let mut tests = 0_usize;
        let mut files = 0_usize;
        for entry in WalkDir::new(&self.root)
            .max_depth(12)
            .follow_links(false)
            .into_iter()
            .filter_entry(is_visible_entry)
        {
            let entry =
                entry.map_err(|error| BrokerError::new("walk_failed", error.to_string()))?;
            if !entry.file_type().is_file() || entry.file_type().is_symlink() {
                continue;
            }
            let relative = entry
                .path()
                .strip_prefix(&self.root)
                .map_err(|_| BrokerError::new("path_escape", "summary path escaped workspace"))?;
            if path_is_sensitive(relative) || path_is_generated(relative) {
                continue;
            }
            files += 1;
            if files > MAX_FILES {
                break;
            }
            let extension = relative
                .extension()
                .and_then(|value| value.to_str())
                .unwrap_or("[none]")
                .to_ascii_lowercase();
            *extensions.entry(extension).or_default() += 1;
            let name = relative
                .file_name()
                .and_then(|value| value.to_str())
                .unwrap_or("");
            if matches!(
                name,
                "pyproject.toml"
                    | "requirements.txt"
                    | "package.json"
                    | "Cargo.toml"
                    | "pubspec.yaml"
                    | "go.mod"
                    | "pom.xml"
                    | "Dockerfile"
                    | "docker-compose.yml"
                    | "docker-compose.yaml"
            ) && manifests.len() < 100
            {
                manifests.push(relative.to_string_lossy().replace('\\', "/"));
            }
            if relative.components().any(|part| {
                matches!(
                    part.as_os_str().to_string_lossy().as_ref(),
                    "test" | "tests" | "spec"
                )
            }) || name.starts_with("test_")
                || name.ends_with("_test.rs")
            {
                tests += 1;
            }
        }
        Ok(json!({
            "file_count": files.min(MAX_FILES),
            "truncated": files > MAX_FILES,
            "extension_counts": extensions,
            "manifests": manifests,
            "test_file_count": tests,
        }))
    }

    fn find_symbols(&self, query: &str, paths: &[String]) -> Result<Value, BrokerError> {
        if query.trim().is_empty()
            || query.len() > 200
            || !query
                .chars()
                .all(|value| value.is_alphanumeric() || matches!(value, '_' | ':' | '.' | '-'))
        {
            return Err(BrokerError::new(
                "invalid_argument",
                "symbol query is invalid",
            ));
        }
        let roots = if paths.is_empty() {
            vec![self.root.clone()]
        } else {
            paths
                .iter()
                .map(|path| self.safe_path(path, true, false))
                .collect::<Result<Vec<_>, _>>()?
        };
        let escaped = regex::escape(query.trim());
        let patterns = [
            format!(r"^\s*(?:async\s+)?def\s+{escaped}\b"),
            format!(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+{escaped}\b"),
            format!(
                r"^\s*(?:export\s+)?(?:async\s+)?(?:function|class|interface|type|const|let|var)\s+{escaped}\b"
            ),
            format!(r"^\s*class\s+{escaped}\b"),
        ];
        let matchers = patterns
            .iter()
            .map(|pattern| {
                RegexBuilder::new(pattern)
                    .case_insensitive(false)
                    .build()
                    .map_err(|error| BrokerError::new("invalid_argument", error.to_string()))
            })
            .collect::<Result<Vec<_>, _>>()?;
        let mut matches = Vec::new();
        for root in roots {
            for entry in WalkDir::new(root)
                .follow_links(false)
                .into_iter()
                .filter_entry(is_visible_entry)
            {
                let entry =
                    entry.map_err(|error| BrokerError::new("walk_failed", error.to_string()))?;
                if !entry.file_type().is_file() || entry.file_type().is_symlink() {
                    continue;
                }
                let relative = entry.path().strip_prefix(&self.root).map_err(|_| {
                    BrokerError::new("path_escape", "symbol path escaped workspace")
                })?;
                if path_is_sensitive(relative)
                    || path_is_generated(relative)
                    || !is_text_candidate(relative)
                {
                    continue;
                }
                if entry
                    .metadata()
                    .map(|item| item.len() as usize > MAX_READ_BYTES)
                    .unwrap_or(true)
                {
                    continue;
                }
                let content = fs::read_to_string(entry.path()).unwrap_or_default();
                for (index, line) in content.lines().enumerate() {
                    if matchers.iter().any(|matcher| matcher.is_match(line)) {
                        matches.push(json!({
                            "path": relative.to_string_lossy().replace('\\', "/"),
                            "line": index + 1,
                            "declaration": bounded_chars(line.trim(), 500),
                        }));
                        if matches.len() >= MAX_SEARCH_MATCHES {
                            break;
                        }
                    }
                }
                if matches.len() >= MAX_SEARCH_MATCHES {
                    break;
                }
            }
            if matches.len() >= MAX_SEARCH_MATCHES {
                break;
            }
        }
        Ok(json!({"matches": matches, "truncated": matches.len() >= MAX_SEARCH_MATCHES}))
    }

    fn apply_exact_patch(
        &self,
        path: &str,
        expected_sha256: &str,
        old: &str,
        replacement: &str,
    ) -> Result<Value, BrokerError> {
        if !self.allow_mutations {
            return Err(BrokerError::new(
                "mutation_denied",
                "structured patches are allowed only inside an ephemeral mutable workspace",
            ));
        }
        if old.is_empty() || old.len() > MAX_READ_BYTES || replacement.len() > MAX_READ_BYTES {
            return Err(BrokerError::new(
                "invalid_argument",
                "patch text must be non-empty and remain within the bounded file limit",
            ));
        }
        if expected_sha256.len() != 64
            || !expected_sha256.bytes().all(|byte| byte.is_ascii_hexdigit())
        {
            return Err(BrokerError::new(
                "invalid_argument",
                "expected_sha256 must be a 64-character hexadecimal digest",
            ));
        }
        let path = self.safe_path(path, true, true)?;
        let metadata = fs::metadata(&path)
            .map_err(|error| BrokerError::new("read_failed", error.to_string()))?;
        if metadata.len() as usize > MAX_READ_BYTES {
            return Err(BrokerError::new(
                "read_limit",
                "file exceeds the bounded patch limit",
            ));
        }
        let content = fs::read_to_string(&path)
            .map_err(|error| BrokerError::new("read_failed", error.to_string()))?;
        let actual_sha256 = sha256_bytes(content.as_bytes());
        if !actual_sha256.eq_ignore_ascii_case(expected_sha256) {
            return Err(BrokerError::new(
                "hash_mismatch",
                "the file changed after the patch was planned",
            ));
        }
        let occurrences = content.match_indices(old).count();
        if occurrences != 1 {
            return Err(BrokerError::new(
                "patch_ambiguous",
                format!("expected the old text exactly once, found {occurrences} occurrences"),
            ));
        }
        let updated = content.replacen(old, replacement, 1);
        if updated.len() > MAX_READ_BYTES {
            return Err(BrokerError::new(
                "write_limit",
                "patched file exceeds the bounded file limit",
            ));
        }
        let parent = path
            .parent()
            .ok_or_else(|| BrokerError::new("write_failed", "file has no parent directory"))?;
        let mut temporary = tempfile::NamedTempFile::new_in(parent)
            .map_err(|error| BrokerError::new("write_failed", error.to_string()))?;
        temporary
            .as_file()
            .set_permissions(metadata.permissions())
            .map_err(|error| BrokerError::new("write_failed", error.to_string()))?;
        temporary
            .write_all(updated.as_bytes())
            .and_then(|_| temporary.as_file().sync_all())
            .map_err(|error| BrokerError::new("write_failed", error.to_string()))?;
        temporary
            .persist(&path)
            .map_err(|error| BrokerError::new("write_failed", error.error.to_string()))?;
        Ok(json!({
            "path": self.relative(&path)?,
            "before_sha256": actual_sha256,
            "after_sha256": sha256_bytes(updated.as_bytes()),
            "bytes": updated.len(),
        }))
    }

    fn create_file(
        &self,
        path: &str,
        expected_absent: bool,
        content: &str,
    ) -> Result<Value, BrokerError> {
        if !self.allow_mutations {
            return Err(BrokerError::new(
                "mutation_denied",
                "new files are allowed only inside an ephemeral mutable workspace",
            ));
        }
        if !expected_absent || content.len() > MAX_READ_BYTES {
            return Err(BrokerError::new(
                "invalid_argument",
                "create_file requires expected_absent=true and bounded content",
            ));
        }
        let path = self.safe_new_path(path)?;
        if path.exists() {
            return Err(BrokerError::new(
                "precondition_failed",
                "new file already exists",
            ));
        }
        let parent = path
            .parent()
            .ok_or_else(|| BrokerError::new("write_failed", "new file has no parent"))?;
        fs::create_dir_all(parent)
            .map_err(|error| BrokerError::new("write_failed", error.to_string()))?;
        let mut temporary = tempfile::NamedTempFile::new_in(parent)
            .map_err(|error| BrokerError::new("write_failed", error.to_string()))?;
        temporary
            .write_all(content.as_bytes())
            .and_then(|_| temporary.as_file().sync_all())
            .map_err(|error| BrokerError::new("write_failed", error.to_string()))?;
        temporary
            .persist_noclobber(&path)
            .map_err(|error| BrokerError::new("write_failed", error.error.to_string()))?;
        Ok(json!({
            "path": self.relative(&path)?,
            "before_sha256": Value::Null,
            "after_sha256": sha256_bytes(content.as_bytes()),
            "bytes": content.len(),
        }))
    }

    fn safe_new_path(&self, raw: &str) -> Result<PathBuf, BrokerError> {
        let candidate = Path::new(raw.trim());
        if candidate.as_os_str().is_empty()
            || candidate.is_absolute()
            || candidate
                .components()
                .any(|part| matches!(part, Component::ParentDir))
            || path_is_sensitive(candidate)
            || path_is_generated(candidate)
        {
            return Err(BrokerError::new("path_denied", "new file path is denied"));
        }
        let mut cursor = self.root.clone();
        for component in candidate.components() {
            let Component::Normal(name) = component else {
                return Err(BrokerError::new("path_denied", "new file path is invalid"));
            };
            cursor.push(name);
            if cursor.exists() {
                let metadata = fs::symlink_metadata(&cursor)
                    .map_err(|error| BrokerError::new("path_invalid", error.to_string()))?;
                if metadata.file_type().is_symlink() {
                    return Err(BrokerError::new(
                        "path_escape",
                        "new file path crosses a symlink",
                    ));
                }
            }
        }
        if !cursor.starts_with(&self.root) {
            return Err(BrokerError::new(
                "path_escape",
                "new file path escapes workspace",
            ));
        }
        Ok(cursor)
    }

    fn git_diff(&self, staged: bool, path: Option<&str>) -> Result<Value, BrokerError> {
        let mut arguments = vec!["diff", "--no-ext-diff", "--no-color"];
        if staged {
            arguments.push("--cached");
        }
        let safe_relative;
        if let Some(path) = path {
            let resolved = self.safe_path(path, false, false)?;
            safe_relative = self.relative(&resolved)?;
            arguments.extend(["--", &safe_relative]);
        }
        self.run_program("git", &arguments, &self.root, 20)
    }

    fn run_validation(
        &self,
        profile: ValidationProfile,
        timeout_seconds: u64,
    ) -> Result<Value, BrokerError> {
        if timeout_seconds == 0 || timeout_seconds > 1_800 {
            return Err(BrokerError::new(
                "invalid_argument",
                "timeout_seconds must be between 1 and 1800",
            ));
        }
        let (program, arguments, relative_cwd): (&str, Vec<&str>, &str) = match profile {
            ValidationProfile::PythonCompile => (
                "python3",
                vec!["-m", "compileall", "-q", "app", "scripts"],
                ".",
            ),
            ValidationProfile::PythonUnit => ("python3", vec!["-m", "pytest", "tests", "-q"], "."),
            ValidationProfile::PythonLint => ("flake8", vec!["app", "--max-line-length=100"], "."),
            ValidationProfile::WebLint => ("npm", vec!["run", "lint"], "web"),
            ValidationProfile::WebBuild => ("npm", vec!["run", "build"], "web"),
            ValidationProfile::FlutterAnalyze => ("flutter", vec!["analyze"], "mobile"),
            ValidationProfile::FlutterTest => ("flutter", vec!["test"], "mobile"),
            ValidationProfile::RustFormat => ("cargo", vec!["fmt", "--check"], "coding_runtime"),
            ValidationProfile::RustTest => ("cargo", vec!["test", "--locked"], "coding_runtime"),
            ValidationProfile::GitDiffCheck => ("git", vec!["diff", "--check"], "."),
        };
        let cwd = self.safe_path(relative_cwd, true, false)?;
        let mut result = self.run_program(program, &arguments, &cwd, timeout_seconds)?;
        if let Value::Object(ref mut object) = result {
            object.insert("profile".to_string(), json!(profile));
        }
        Ok(result)
    }

    fn run_program(
        &self,
        program: &str,
        arguments: &[&str],
        cwd: &Path,
        timeout_seconds: u64,
    ) -> Result<Value, BrokerError> {
        let allowed = ["git", "python3", "flake8", "npm", "flutter", "cargo"];
        if !allowed.contains(&program) {
            return Err(BrokerError::new(
                "command_denied",
                "program is not allowlisted",
            ));
        }
        let path =
            std::env::var("PATH").unwrap_or_else(|_| "/usr/local/bin:/usr/bin:/bin".to_string());
        let clean_home = TempDirBuilder::new()
            .prefix("coding-runtime-home-")
            .tempdir()
            .map_err(|error| BrokerError::new("sandbox_setup_failed", error.to_string()))?;
        let mut child = Command::new(program)
            .args(arguments)
            .current_dir(cwd)
            .env_clear()
            .env("PATH", path)
            .env("HOME", clean_home.path())
            .env("CI", "true")
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|error| BrokerError::new("command_start_failed", error.to_string()))?;
        let stdout_reader = spawn_bounded_reader(child.stdout.take(), self.max_output_bytes);
        let stderr_reader = spawn_bounded_reader(child.stderr.take(), self.max_output_bytes);
        let status = child
            .wait_timeout(Duration::from_secs(timeout_seconds))
            .map_err(|error| BrokerError::new("command_wait_failed", error.to_string()))?;
        if status.is_none() {
            let _ = child.kill();
            let _ = child.wait();
            return Err(BrokerError::new(
                "command_timeout",
                format!("validation exceeded {timeout_seconds} seconds"),
            ));
        }
        let status = status.expect("checked above");
        let stdout = join_bounded_reader(stdout_reader)?;
        let stderr = join_bounded_reader(stderr_reader)?;
        Ok(json!({
            "program": program,
            "arguments": arguments,
            "cwd": self.relative(cwd)?,
            "exit_code": status.code(),
            "success": status.success(),
            "stdout": stdout,
            "stderr": stderr,
        }))
    }

    fn safe_path(
        &self,
        raw: &str,
        must_exist: bool,
        must_be_file: bool,
    ) -> Result<PathBuf, BrokerError> {
        let raw = if raw.trim().is_empty() {
            "."
        } else {
            raw.trim()
        };
        let candidate = Path::new(raw);
        if candidate.is_absolute()
            || candidate
                .components()
                .any(|part| matches!(part, Component::ParentDir))
        {
            return Err(BrokerError::new(
                "path_denied",
                "absolute paths and parent traversal are denied",
            ));
        }
        if path_is_sensitive(candidate) {
            return Err(BrokerError::new(
                "sensitive_path",
                "credential and environment paths are denied",
            ));
        }
        let joined = self.root.join(candidate);
        let resolved = if joined.exists() {
            joined
                .canonicalize()
                .map_err(|error| BrokerError::new("path_invalid", error.to_string()))?
        } else {
            let parent = joined
                .parent()
                .ok_or_else(|| BrokerError::new("path_invalid", "path has no parent"))?;
            parent
                .canonicalize()
                .map_err(|error| BrokerError::new("path_invalid", error.to_string()))?
                .join(
                    joined
                        .file_name()
                        .ok_or_else(|| BrokerError::new("path_invalid", "path has no filename"))?,
                )
        };
        if !resolved.starts_with(&self.root) {
            return Err(BrokerError::new(
                "path_escape",
                "resolved path escapes workspace",
            ));
        }
        if must_exist && !resolved.exists() {
            return Err(BrokerError::new(
                "path_missing",
                "requested path does not exist",
            ));
        }
        if must_be_file && !resolved.is_file() {
            return Err(BrokerError::new(
                "path_invalid",
                "requested path is not a regular file",
            ));
        }
        Ok(resolved)
    }

    fn relative(&self, path: &Path) -> Result<String, BrokerError> {
        let relative = path
            .strip_prefix(&self.root)
            .map_err(|_| BrokerError::new("path_escape", "path escapes workspace"))?;
        let value = relative.to_string_lossy().replace('\\', "/");
        Ok(if value.is_empty() {
            ".".to_string()
        } else {
            value
        })
    }
}

impl ToolRequest {
    fn tool_name(&self) -> &'static str {
        match self {
            Self::Inventory { .. } => "inventory",
            Self::SearchLiteral { .. } => "search_literal",
            Self::ReadLines { .. } => "read_lines",
            Self::HashFile { .. } => "hash_file",
            Self::ProjectSummary => "project_summary",
            Self::FindSymbols { .. } => "find_symbols",
            Self::ApplyExactPatch { .. } => "apply_exact_patch",
            Self::CreateFile { .. } => "create_file",
            Self::GitStatus => "git_status",
            Self::GitDiff { .. } => "git_diff",
            Self::RunValidation { .. } => "run_validation",
        }
    }
}

fn default_depth() -> usize {
    8
}
fn default_timeout() -> u64 {
    300
}

fn is_visible_entry(entry: &DirEntry) -> bool {
    if entry.depth() == 0 {
        return true;
    }
    let name = entry.file_name().to_string_lossy();
    !(name.starts_with('.') && name != ".github")
        && !matches!(
            name.as_ref(),
            "node_modules" | "build" | "dist" | "target" | "__pycache__" | ".venv" | "venv"
        )
}

pub fn path_is_sensitive(path: &Path) -> bool {
    path.components().any(|component| {
        let value = component.as_os_str().to_string_lossy().to_ascii_lowercase();
        value == ".env"
            || value.starts_with(".env.")
            || value.contains("credential")
            || value == "token.json"
            || value == "token.pkl"
            || value.ends_with(".pem")
            || value.ends_with(".key")
            || value == ".ssh"
            || value == ".aws"
            || value == ".gnupg"
    })
}

pub fn path_is_generated(path: &Path) -> bool {
    path.components().any(|component| {
        matches!(
            component.as_os_str().to_string_lossy().as_ref(),
            ".git"
                | ".gca-local"
                | "node_modules"
                | "build"
                | "dist"
                | "target"
                | "__pycache__"
                | ".venv"
                | "venv"
        )
    })
}

pub fn sha256_bytes(value: &[u8]) -> String {
    format!("{:x}", Sha256::digest(value))
}

fn is_text_candidate(path: &Path) -> bool {
    matches!(
        path.extension()
            .and_then(|value| value.to_str())
            .unwrap_or(""),
        "py" | "rs"
            | "toml"
            | "json"
            | "yaml"
            | "yml"
            | "md"
            | "txt"
            | "js"
            | "jsx"
            | "ts"
            | "tsx"
            | "dart"
            | "sql"
            | "sh"
            | "html"
            | "css"
    )
}

fn bounded_chars(value: &str, max_chars: usize) -> String {
    value.chars().take(max_chars).collect()
}

type ReaderResult = Result<(Vec<u8>, bool), String>;

fn spawn_bounded_reader<T: Read + Send + 'static>(
    stream: Option<T>,
    limit: usize,
) -> Option<JoinHandle<ReaderResult>> {
    stream.map(|mut stream| {
        thread::spawn(move || {
            let mut retained = Vec::new();
            let mut buffer = [0_u8; 8_192];
            let mut truncated = false;
            loop {
                let count = stream
                    .read(&mut buffer)
                    .map_err(|error| error.to_string())?;
                if count == 0 {
                    break;
                }
                let available = limit.saturating_sub(retained.len());
                let keep = available.min(count);
                retained.extend_from_slice(&buffer[..keep]);
                truncated |= keep < count;
            }
            Ok((retained, truncated))
        })
    })
}

fn join_bounded_reader(reader: Option<JoinHandle<ReaderResult>>) -> Result<String, BrokerError> {
    let Some(reader) = reader else {
        return Ok(String::new());
    };
    let (bytes, truncated) = reader
        .join()
        .map_err(|_| BrokerError::new("output_read_failed", "output reader terminated"))?
        .map_err(|error| BrokerError::new("output_read_failed", error))?;
    let mut value = String::from_utf8_lossy(&bytes).to_string();
    if truncated {
        value.push_str("\n[output truncated]");
    }
    Ok(value)
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    fn fixture() -> (TempDir, Broker) {
        let temp = TempDir::new().unwrap();
        fs::create_dir(temp.path().join("app")).unwrap();
        fs::write(
            temp.path().join("app/main.py"),
            "def hello():\n    return 'world'\n",
        )
        .unwrap();
        fs::write(temp.path().join(".env"), "SECRET=never-read\n").unwrap();
        let broker = Broker::new(temp.path()).unwrap();
        (temp, broker)
    }

    #[test]
    fn inventory_excludes_sensitive_files() {
        let (_temp, broker) = fixture();
        let response = broker.execute(ToolRequest::Inventory {
            path: "".into(),
            max_depth: 4,
        });
        assert!(response.ok);
        let rendered = response.result.to_string();
        assert!(rendered.contains("app/main.py"));
        assert!(!rendered.contains(".env"));
    }

    #[test]
    fn read_rejects_traversal_and_secrets() {
        let (_temp, broker) = fixture();
        let traversal = broker.execute(ToolRequest::ReadLines {
            path: "../outside".into(),
            start_line: 1,
            end_line: 2,
        });
        assert!(!traversal.ok);
        let secret = broker.execute(ToolRequest::ReadLines {
            path: ".env".into(),
            start_line: 1,
            end_line: 2,
        });
        assert!(!secret.ok);
    }

    #[test]
    fn literal_search_returns_bounded_locations() {
        let (_temp, broker) = fixture();
        let response = broker.execute(ToolRequest::SearchLiteral {
            query: "hello".into(),
            paths: vec!["app".into()],
            case_sensitive: true,
        });
        assert!(response.ok);
        assert_eq!(response.result["matches"][0]["line"], 1);
    }

    #[test]
    fn unknown_json_fields_are_rejected() {
        let parsed = parse_request(r#"{"tool":"git_status","command":"rm -rf /"}"#);
        assert!(parsed.is_err());
    }

    #[test]
    fn exact_patch_requires_mutable_ephemeral_broker() {
        let (temp, broker) = fixture();
        let source = fs::read(temp.path().join("app/main.py")).unwrap();
        let request = || ToolRequest::ApplyExactPatch {
            path: "app/main.py".into(),
            expected_sha256: sha256_bytes(&source),
            old: "return 'world'".into(),
            replacement: "return 'local'".into(),
        };
        let denied = broker.execute(request());
        assert!(!denied.ok);
        assert_eq!(denied.error.unwrap().code, "mutation_denied");

        let mutable = Broker::new_mutable(temp.path()).unwrap();
        let applied = mutable.execute(request());
        assert!(applied.ok);
        assert!(
            fs::read_to_string(temp.path().join("app/main.py"))
                .unwrap()
                .contains("return 'local'")
        );
    }

    #[test]
    fn summary_and_symbol_lookup_are_bounded_and_source_grounded() {
        let (_temp, broker) = fixture();
        let summary = broker.execute(ToolRequest::ProjectSummary);
        assert!(summary.ok);
        assert_eq!(summary.result["extension_counts"]["py"], 1);
        let symbols = broker.execute(ToolRequest::FindSymbols {
            query: "hello".into(),
            paths: vec!["app".into()],
        });
        assert!(symbols.ok);
        assert_eq!(symbols.result["matches"][0]["line"], 1);
    }

    #[test]
    fn new_file_requires_mutable_broker_and_absence_precondition() {
        let (temp, broker) = fixture();
        let request = || ToolRequest::CreateFile {
            path: "tests/test_new.py".into(),
            expected_absent: true,
            content: "def test_new():\n    assert True\n".into(),
        };
        let denied = broker.execute(request());
        assert!(!denied.ok);
        let mutable = Broker::new_mutable(temp.path()).unwrap();
        assert!(mutable.execute(request()).ok);
        let duplicate = mutable.execute(request());
        assert!(!duplicate.ok);
        assert_eq!(duplicate.error.unwrap().code, "precondition_failed");
    }
}
