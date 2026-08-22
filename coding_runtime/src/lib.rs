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
    LanguageInventory {
        #[serde(default)]
        paths: Vec<String>,
    },
    DependencyInventory {
        #[serde(default)]
        paths: Vec<String>,
    },
    ComplexityInventory {
        #[serde(default)]
        paths: Vec<String>,
    },
    ConversionContract {
        source_path: String,
        target_language: String,
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
        "language_inventory" => &["tool", "paths"],
        "dependency_inventory" => &["tool", "paths"],
        "complexity_inventory" => &["tool", "paths"],
        "conversion_contract" => &["tool", "source_path", "target_language"],
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
            ToolRequest::LanguageInventory { paths } => self.language_inventory(&paths),
            ToolRequest::DependencyInventory { paths } => self.dependency_inventory(&paths),
            ToolRequest::ComplexityInventory { paths } => self.complexity_inventory(&paths),
            ToolRequest::ConversionContract {
                source_path,
                target_language,
            } => self.conversion_contract(&source_path, &target_language),
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

    fn analysis_files(&self, paths: &[String]) -> Result<(Vec<PathBuf>, bool), BrokerError> {
        let roots = if paths.is_empty() {
            vec![self.root.clone()]
        } else {
            paths
                .iter()
                .map(|path| self.safe_path(path, true, false))
                .collect::<Result<Vec<_>, _>>()?
        };
        let mut files = Vec::new();
        let mut seen = BTreeSet::new();
        let mut truncated = false;
        for root in roots {
            let entries: Box<dyn Iterator<Item = Result<DirEntry, walkdir::Error>>> =
                if root.is_file() {
                    Box::new(WalkDir::new(root).max_depth(0).into_iter())
                } else {
                    Box::new(
                        WalkDir::new(root)
                            .max_depth(20)
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
                    BrokerError::new("path_escape", "analysis path escaped workspace")
                })?;
                if path_is_sensitive(relative)
                    || path_is_generated(relative)
                    || !is_text_candidate(relative)
                    || !seen.insert(relative.to_path_buf())
                {
                    continue;
                }
                let within_limit = entry
                    .metadata()
                    .map(|metadata| metadata.len() as usize <= MAX_READ_BYTES)
                    .unwrap_or(false);
                if !within_limit {
                    continue;
                }
                files.push(entry.path().to_path_buf());
                if files.len() >= MAX_FILES {
                    truncated = true;
                    break;
                }
            }
            if truncated {
                break;
            }
        }
        Ok((files, truncated))
    }

    fn language_inventory(&self, paths: &[String]) -> Result<Value, BrokerError> {
        let (files, truncated) = self.analysis_files(paths)?;
        let mut languages = std::collections::BTreeMap::<String, Value>::new();
        let mut analyzed_files = 0_usize;
        for path in files {
            let relative = path
                .strip_prefix(&self.root)
                .map_err(|_| BrokerError::new("path_escape", "language path escaped"))?;
            let Some(language) = source_language(relative) else {
                continue;
            };
            let content = fs::read_to_string(&path).unwrap_or_default();
            analyzed_files += 1;
            let current = languages
                .entry(language.to_string())
                .or_insert_with(|| json!({"files":0,"bytes":0,"lines":0}));
            current["files"] = json!(current["files"].as_u64().unwrap_or(0) + 1);
            current["bytes"] = json!(current["bytes"].as_u64().unwrap_or(0) + content.len() as u64);
            current["lines"] =
                json!(current["lines"].as_u64().unwrap_or(0) + content.lines().count() as u64);
        }
        Ok(json!({
            "analysis_kind":"extension_grounded_language_inventory",
            "languages":languages,
            "analyzed_files":analyzed_files,
            "truncated":truncated,
            "semantic_claim":false,
        }))
    }

    fn dependency_inventory(&self, paths: &[String]) -> Result<Value, BrokerError> {
        let (files, file_truncated) = self.analysis_files(paths)?;
        let mut edges = Vec::new();
        let mut truncated = file_truncated;
        for path in files {
            let relative = path
                .strip_prefix(&self.root)
                .map_err(|_| BrokerError::new("path_escape", "dependency path escaped"))?;
            let Some(language) = source_language(relative) else {
                continue;
            };
            let content = fs::read_to_string(&path).unwrap_or_default();
            for (index, line) in content.lines().enumerate() {
                for (kind, target) in lexical_dependencies(language, line) {
                    edges.push(json!({
                        "path":relative.to_string_lossy().replace('\\', "/"),
                        "language":language,
                        "line":index + 1,
                        "kind":kind,
                        "target":bounded_chars(&target, 300),
                    }));
                    if edges.len() >= MAX_SEARCH_MATCHES {
                        truncated = true;
                        break;
                    }
                }
                if edges.len() >= MAX_SEARCH_MATCHES {
                    break;
                }
            }
            if edges.len() >= MAX_SEARCH_MATCHES {
                break;
            }
        }
        Ok(json!({
            "analysis_kind":"bounded_lexical_dependency_inventory",
            "edges":edges,
            "truncated":truncated,
            "complete_static_graph":false,
            "warning":"Dynamic imports, generated code, runtime dispatch, and language-specific resolution require dedicated analyzers.",
        }))
    }

    fn complexity_inventory(&self, paths: &[String]) -> Result<Value, BrokerError> {
        let (files, file_truncated) = self.analysis_files(paths)?;
        let declaration = RegexBuilder::new(
            r"(?x)^\s*(?:pub(?:\([^)]*\))?\s+|export\s+|async\s+|static\s+)*
              (?:def|fn|function|class|interface|func)\s+([A-Za-z_][A-Za-z0-9_]*)",
        )
        .build()
        .map_err(|error| BrokerError::new("analysis_failed", error.to_string()))?;
        let decisions = RegexBuilder::new(
            r"\b(?:if|elif|else\s+if|for|while|match|case|catch|except|when|switch)\b|&&|\|\|",
        )
        .build()
        .map_err(|error| BrokerError::new("analysis_failed", error.to_string()))?;
        let mut results = Vec::new();
        let mut truncated = file_truncated;
        for path in files {
            let relative = path
                .strip_prefix(&self.root)
                .map_err(|_| BrokerError::new("path_escape", "complexity path escaped"))?;
            let Some(language) = source_language(relative) else {
                continue;
            };
            let content = fs::read_to_string(&path).unwrap_or_default();
            let mut names = Vec::new();
            for line in content.lines() {
                if let Some(capture) = declaration.captures(line)
                    && let Some(name) = capture.get(1)
                    && names.len() < 200
                {
                    names.push(name.as_str().to_string());
                }
            }
            let recursion_candidates = names
                .iter()
                .filter(|name| content.match_indices(name.as_str()).count() > 1)
                .take(50)
                .cloned()
                .collect::<Vec<_>>();
            results.push(json!({
                "path":relative.to_string_lossy().replace('\\', "/"),
                "language":language,
                "lines":content.lines().count(),
                "non_blank_lines":content.lines().filter(|line| !line.trim().is_empty()).count(),
                "declarations":names,
                "decision_markers":decisions.find_iter(&content).count(),
                "recursion_candidates":recursion_candidates,
            }));
            if results.len() >= MAX_SEARCH_MATCHES {
                truncated = true;
                break;
            }
        }
        Ok(json!({
            "analysis_kind":"bounded_lexical_complexity_inventory",
            "files":results,
            "truncated":truncated,
            "big_o_inferred":false,
            "cyclomatic_complexity_proven":false,
            "warning":"Counts are localization evidence, not semantic complexity proofs; inspect exact control flow and benchmarks before changing algorithms.",
        }))
    }

    fn conversion_contract(
        &self,
        source_path: &str,
        target_language: &str,
    ) -> Result<Value, BrokerError> {
        let path = self.safe_path(source_path, true, true)?;
        let relative = path
            .strip_prefix(&self.root)
            .map_err(|_| BrokerError::new("path_escape", "conversion path escaped"))?;
        let source = source_language(relative).ok_or_else(|| {
            BrokerError::new("unsupported_language", "source language is not recognized")
        })?;
        let target = normalize_target_language(target_language).ok_or_else(|| {
            BrokerError::new("unsupported_language", "target language is not recognized")
        })?;
        if source == target {
            return Err(BrokerError::new(
                "invalid_argument",
                "source and target languages are the same",
            ));
        }
        let content = fs::read_to_string(&path)
            .map_err(|error| BrokerError::new("read_failed", error.to_string()))?;
        let feature_terms = [
            ("async", ["async ", "await "].as_slice()),
            (
                "exceptions",
                ["try", "catch", "except", "throw", "raise"].as_slice(),
            ),
            (
                "generics",
                ["<T", "typing.", "TypeVar", "where T"].as_slice(),
            ),
            (
                "macros_or_codegen",
                ["macro!", "#[derive", "@generated"].as_slice(),
            ),
            (
                "unsafe_or_ffi",
                ["unsafe", "extern ", "ffi", "ctypes"].as_slice(),
            ),
            (
                "concurrency",
                ["thread", "spawn", "asyncio", "goroutine", "channel"].as_slice(),
            ),
        ];
        let features = feature_terms
            .iter()
            .filter(|(_, terms)| terms.iter().any(|term| content.contains(term)))
            .map(|(name, _)| *name)
            .collect::<Vec<_>>();
        let validation_profiles = target_validation_profiles(target);
        Ok(json!({
            "analysis_kind":"hash_bound_conversion_contract",
            "source_path":relative.to_string_lossy().replace('\\', "/"),
            "source_language":source,
            "target_language":target,
            "source_sha256":sha256_bytes(content.as_bytes()),
            "source_features":features,
            "required_evidence":[
                "target parser or compiler passes",
                "source and target behavior use the same fixtures",
                "differential tests pass for normal, boundary, and failure cases",
                "public API and side-effect differences are reviewed",
            ],
            "available_validation_profiles":validation_profiles,
            "semantic_equivalence_proven":false,
            "automatic_conversion_performed":false,
            "mutation_boundary":"Only hash-bound apply_exact_patch/create_file actions in an ephemeral sandbox may implement the conversion.",
        }))
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
            Self::LanguageInventory { .. } => "language_inventory",
            Self::DependencyInventory { .. } => "dependency_inventory",
            Self::ComplexityInventory { .. } => "complexity_inventory",
            Self::ConversionContract { .. } => "conversion_contract",
            Self::ApplyExactPatch { .. } => "apply_exact_patch",
            Self::CreateFile { .. } => "create_file",
            Self::GitStatus => "git_status",
            Self::GitDiff { .. } => "git_diff",
            Self::RunValidation { .. } => "run_validation",
        }
    }
}

fn source_language(path: &Path) -> Option<&'static str> {
    let name = path.file_name()?.to_string_lossy().to_ascii_lowercase();
    if name == "dockerfile" {
        return Some("dockerfile");
    }
    let extension = path.extension()?.to_string_lossy().to_ascii_lowercase();
    match extension.as_str() {
        "py" | "pyi" => Some("python"),
        "rs" => Some("rust"),
        "ts" | "tsx" => Some("typescript"),
        "js" | "jsx" | "mjs" | "cjs" => Some("javascript"),
        "go" => Some("go"),
        "java" => Some("java"),
        "kt" | "kts" => Some("kotlin"),
        "dart" => Some("dart"),
        "cs" => Some("csharp"),
        "cpp" | "cc" | "cxx" | "hpp" | "hh" | "hxx" => Some("cpp"),
        "c" | "h" => Some("c"),
        "rb" => Some("ruby"),
        "php" => Some("php"),
        "sh" | "bash" | "zsh" => Some("shell"),
        "sql" => Some("sql"),
        "yaml" | "yml" => Some("yaml"),
        "toml" => Some("toml"),
        "json" => Some("json"),
        "md" | "mdx" => Some("markdown"),
        _ => None,
    }
}

fn normalize_target_language(value: &str) -> Option<&'static str> {
    match value.trim().to_ascii_lowercase().as_str() {
        "python" | "py" => Some("python"),
        "rust" | "rs" => Some("rust"),
        "typescript" | "ts" => Some("typescript"),
        "javascript" | "js" | "node" | "nodejs" => Some("javascript"),
        "go" | "golang" => Some("go"),
        "java" => Some("java"),
        "kotlin" | "kt" => Some("kotlin"),
        "dart" | "flutter" => Some("dart"),
        "c#" | "csharp" | "cs" => Some("csharp"),
        "c++" | "cpp" => Some("cpp"),
        "c" => Some("c"),
        "ruby" | "rb" => Some("ruby"),
        "php" => Some("php"),
        _ => None,
    }
}

fn target_validation_profiles(language: &str) -> Vec<&'static str> {
    match language {
        "python" => vec!["python_compile", "python_lint", "python_unit"],
        "rust" => vec!["rust_format", "rust_test"],
        "typescript" | "javascript" => vec!["web_lint", "web_build"],
        "dart" => vec!["flutter_analyze", "flutter_test"],
        _ => Vec::new(),
    }
}

fn first_quoted(value: &str) -> Option<String> {
    let candidate = ['\'', '"']
        .into_iter()
        .filter_map(|quote| value.find(quote).map(|start| (start, quote)))
        .min_by_key(|(start, _)| *start);
    if let Some((start, quote)) = candidate {
        let rest = &value[start + quote.len_utf8()..];
        if let Some(end) = rest.find(quote) {
            return Some(rest[..end].to_string());
        }
    }
    if let Some(start) = value.find('<') {
        let rest = &value[start + 1..];
        if let Some(end) = rest.find('>') {
            return Some(rest[..end].to_string());
        }
    }
    None
}

fn lexical_dependencies(language: &str, line: &str) -> Vec<(&'static str, String)> {
    let trimmed = line.trim();
    if trimmed.is_empty()
        || trimmed.starts_with("//")
        || trimmed.starts_with('#') && !matches!(language, "c" | "cpp")
    {
        return Vec::new();
    }
    match language {
        "python" if trimmed.starts_with("from ") => trimmed
            .strip_prefix("from ")
            .and_then(|rest| rest.split_whitespace().next())
            .map(|target| vec![("from_import", target.to_string())])
            .unwrap_or_default(),
        "python" if trimmed.starts_with("import ") => trimmed
            .trim_start_matches("import ")
            .split(',')
            .filter_map(|item| item.split_whitespace().next())
            .map(|target| ("import", target.to_string()))
            .collect(),
        "typescript" | "javascript" if trimmed.starts_with("import ") => first_quoted(trimmed)
            .map(|target| vec![("import", target)])
            .unwrap_or_default(),
        "typescript" | "javascript" if trimmed.contains("require(") => first_quoted(trimmed)
            .map(|target| vec![("require", target)])
            .unwrap_or_default(),
        "rust" if trimmed.starts_with("use ") => vec![(
            "use",
            trimmed
                .trim_start_matches("use ")
                .trim_end_matches(';')
                .to_string(),
        )],
        "rust" if trimmed.starts_with("mod ") => vec![(
            "module",
            trimmed
                .trim_start_matches("mod ")
                .trim_end_matches(';')
                .to_string(),
        )],
        "go" if trimmed.starts_with("import ") => first_quoted(trimmed)
            .map(|target| vec![("import", target)])
            .unwrap_or_default(),
        "java" | "kotlin" if trimmed.starts_with("import ") => vec![(
            "import",
            trimmed
                .trim_start_matches("import ")
                .trim_end_matches(';')
                .to_string(),
        )],
        "dart" if trimmed.starts_with("import ") => first_quoted(trimmed)
            .map(|target| vec![("import", target)])
            .unwrap_or_default(),
        "csharp" if trimmed.starts_with("using ") => vec![(
            "using",
            trimmed
                .trim_start_matches("using ")
                .trim_end_matches(';')
                .to_string(),
        )],
        "c" | "cpp" if trimmed.starts_with("#include") => first_quoted(trimmed)
            .map(|target| vec![("include", target)])
            .unwrap_or_default(),
        "ruby" if trimmed.starts_with("require ") => first_quoted(trimmed)
            .map(|target| vec![("require", target)])
            .unwrap_or_default(),
        "php" if trimmed.starts_with("use ") => vec![(
            "use",
            trimmed
                .trim_start_matches("use ")
                .trim_end_matches(';')
                .to_string(),
        )],
        _ => Vec::new(),
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
    fn language_dependency_and_complexity_tools_return_bounded_evidence() {
        let (temp, broker) = fixture();
        fs::write(
            temp.path().join("app/worker.ts"),
            "import { run } from './runtime';\nexport function choose(value: number) {\n  if (value > 1) return run(value);\n  return value;\n}\n",
        )
        .unwrap();
        let languages = broker.execute(ToolRequest::LanguageInventory {
            paths: vec!["app".into()],
        });
        assert!(languages.ok);
        assert_eq!(languages.result["languages"]["python"]["files"], 1);
        assert_eq!(languages.result["languages"]["typescript"]["files"], 1);

        let dependencies = broker.execute(ToolRequest::DependencyInventory {
            paths: vec!["app".into()],
        });
        assert!(dependencies.ok);
        assert_eq!(dependencies.result["complete_static_graph"], false);
        assert_eq!(dependencies.result["edges"][0]["target"], "./runtime");

        let complexity = broker.execute(ToolRequest::ComplexityInventory {
            paths: vec!["app/worker.ts".into()],
        });
        assert!(complexity.ok);
        assert_eq!(complexity.result["big_o_inferred"], false);
        assert_eq!(complexity.result["cyclomatic_complexity_proven"], false);
        assert_eq!(complexity.result["files"][0]["decision_markers"], 1);
    }

    #[test]
    fn conversion_contract_is_hash_bound_and_never_claims_equivalence() {
        let (_temp, broker) = fixture();
        let contract = broker.execute(ToolRequest::ConversionContract {
            source_path: "app/main.py".into(),
            target_language: "Rust".into(),
        });
        assert!(contract.ok);
        assert_eq!(contract.result["source_language"], "python");
        assert_eq!(contract.result["target_language"], "rust");
        assert_eq!(contract.result["semantic_equivalence_proven"], false);
        assert_eq!(contract.result["automatic_conversion_performed"], false);
        assert_eq!(
            contract.result["available_validation_profiles"][1],
            "rust_test"
        );
        assert_eq!(contract.result["source_sha256"].as_str().unwrap().len(), 64);
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
