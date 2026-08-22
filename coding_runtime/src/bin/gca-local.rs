use google_connector_coding_runtime::{
    Broker, ToolRequest, path_is_generated, path_is_sensitive, sha256_bytes,
};
use serde::Deserialize;
use serde_json::{Value, json};
use std::collections::BTreeMap;
use std::fs;
use std::io::{Read, Write};
use std::path::{Component, Path, PathBuf};
use tempfile::TempDir;
use walkdir::WalkDir;

const MAX_PLAN_BYTES: usize = 1_048_576;
const MAX_ACTIONS: usize = 50;
const MAX_COPY_FILES: usize = 20_000;
const MAX_COPY_BYTES: u64 = 536_870_912;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ExecutionPlan {
    actions: Vec<ToolRequest>,
}

fn main() {
    if let Err(error) = run() {
        println!("{}", json!({"ok": false, "error": error}));
        std::process::exit(1);
    }
}

fn run() -> Result<(), String> {
    let arguments = std::env::args().skip(1).collect::<Vec<_>>();
    let command = arguments.first().map(String::as_str).unwrap_or("");
    match command {
        "doctor" => {
            reject_unknown_arguments(&arguments, &["--workspace"])?;
            let workspace = required_value(&arguments, "--workspace")?;
            let root = canonical_workspace(workspace)?;
            let git = root.join(".git").exists();
            println!(
                "{}",
                json!({
                    "ok": true,
                    "mode": "local_private_workspace",
                    "workspace": root,
                    "git_repository": git,
                    "model_has_shell": false,
                    "model_has_credentials": false,
                    "writes_require_approval_token": true,
                })
            );
            Ok(())
        }
        "execute-plan" => {
            reject_unknown_arguments(&arguments, &["--workspace", "--plan", "--approve"])?;
            execute_plan(&arguments)
        }
        _ => Err(
            "usage: gca-local doctor --workspace <path> | gca-local execute-plan --workspace <path> --plan <file> [--approve <plan-sha256>]"
                .to_string(),
        ),
    }
}

fn execute_plan(arguments: &[String]) -> Result<(), String> {
    let root = canonical_workspace(required_value(arguments, "--workspace")?)?;
    let plan_path = PathBuf::from(required_value(arguments, "--plan")?);
    let approval = optional_value(arguments, "--approve")?;
    let plan_bytes = read_bounded(&plan_path, MAX_PLAN_BYTES)?;
    let approval_token = sha256_bytes(&plan_bytes);
    if let Some(provided) = approval
        && provided != approval_token
    {
        return Err("approval token does not match the exact execution plan".to_string());
    }
    let plan: ExecutionPlan = serde_json::from_slice(&plan_bytes)
        .map_err(|error| format!("plan violates the typed schema: {error}"))?;
    if plan.actions.is_empty() || plan.actions.len() > MAX_ACTIONS {
        return Err(format!(
            "plan must contain between 1 and {MAX_ACTIONS} actions"
        ));
    }
    let last_patch = plan
        .actions
        .iter()
        .rposition(|action| matches!(action, ToolRequest::ApplyExactPatch { .. }));
    let last_validation = plan
        .actions
        .iter()
        .rposition(|action| matches!(action, ToolRequest::RunValidation { .. }));
    if let Some(patch_index) = last_patch
        && !last_validation.is_some_and(|validation_index| validation_index > patch_index)
    {
        return Err(
            "a fixed validation profile must run after the final structured patch".to_string(),
        );
    }

    let sandbox = TempDir::new().map_err(|error| format!("cannot create sandbox: {error}"))?;
    copy_workspace(&root, sandbox.path())?;
    let broker = Broker::new_mutable(sandbox.path())
        .map_err(|error| format!("cannot initialize sandbox broker: {error:?}"))?;
    let mut original_hashes = BTreeMap::<String, String>::new();
    let mut action_results = Vec::<Value>::new();

    for action in plan.actions {
        if let ToolRequest::ApplyExactPatch { path, .. } = &action {
            validate_relative_path(path)?;
            original_hashes.entry(path.clone()).or_insert_with(|| {
                fs::read(root.join(path))
                    .map(|bytes| sha256_bytes(&bytes))
                    .unwrap_or_default()
            });
        }
        let response = broker.execute(action);
        let validation_failed = response.tool == "run_validation"
            && response.result.get("success").and_then(Value::as_bool) == Some(false);
        let serialized = serde_json::to_value(&response)
            .map_err(|error| format!("cannot serialize tool result: {error}"))?;
        action_results.push(serialized);
        if !response.ok || validation_failed {
            println!(
                "{}",
                json!({
                    "ok": false,
                    "approval_token": approval_token,
                    "original_workspace_modified": false,
                    "actions": action_results,
                    "error": "sandbox action or validation failed",
                })
            );
            return Err("execution stopped safely inside the ephemeral sandbox".to_string());
        }
    }

    let changes = collect_changes(sandbox.path(), &original_hashes)?;
    if approval.is_none() {
        println!(
            "{}",
            json!({
                "ok": true,
                "status": "awaiting_local_approval",
                "approval_token": approval_token,
                "original_workspace_modified": false,
                "changes": changes,
                "actions": action_results,
                "next_command": format!(
                    "gca-local execute-plan --workspace <path> --plan <file> --approve {approval_token}"
                ),
            })
        );
        return Ok(());
    }

    let mut prepared = Vec::new();
    for (path, expected_hash) in &original_hashes {
        if expected_hash.is_empty() {
            return Err(format!("patch target is unavailable: {path}"));
        }
        let destination = safe_original_file(&root, path)?;
        let current =
            fs::read(&destination).map_err(|error| format!("cannot re-read {path}: {error}"))?;
        if sha256_bytes(&current) != *expected_hash {
            return Err(format!(
                "{path} changed after planning; no files were written"
            ));
        }
        let updated = fs::read(sandbox.path().join(path))
            .map_err(|error| format!("cannot read validated sandbox file {path}: {error}"))?;
        prepared.push((path.clone(), destination, current, updated));
    }
    for (applied, (path, destination, _, updated)) in prepared.iter().enumerate() {
        if let Err(error) = atomic_replace_bytes(updated, destination) {
            let mut rollback_errors = Vec::new();
            for (rollback_path, rollback_destination, preimage, _) in
                prepared[..applied].iter().rev()
            {
                if let Err(rollback_error) = atomic_replace_bytes(preimage, rollback_destination) {
                    rollback_errors.push(format!("{rollback_path}: {rollback_error}"));
                }
            }
            if rollback_errors.is_empty() {
                return Err(format!(
                    "could not apply {path}: {error}; earlier files were rolled back"
                ));
            }
            return Err(format!(
                "could not apply {path}: {error}; rollback needs manual reconciliation: {}",
                rollback_errors.join("; ")
            ));
        }
    }
    println!(
        "{}",
        json!({
            "ok": true,
            "status": "applied",
            "approval_token": approval_token,
            "original_workspace_modified": true,
            "changes": changes,
            "actions": action_results,
        })
    );
    Ok(())
}

fn required_value<'a>(arguments: &'a [String], flag: &str) -> Result<&'a str, String> {
    optional_value(arguments, flag)?.ok_or_else(|| format!("missing required argument {flag}"))
}

fn reject_unknown_arguments(arguments: &[String], allowed: &[&str]) -> Result<(), String> {
    let mut index = 1;
    while index < arguments.len() {
        let flag = arguments[index].as_str();
        if !allowed.contains(&flag) {
            return Err(format!("unknown argument {flag}"));
        }
        if arguments.get(index + 1).is_none() {
            return Err(format!("missing value for {flag}"));
        }
        index += 2;
    }
    Ok(())
}

fn optional_value<'a>(arguments: &'a [String], flag: &str) -> Result<Option<&'a str>, String> {
    let mut found = None;
    let mut index = 1;
    while index < arguments.len() {
        if arguments[index] == flag {
            let value = arguments
                .get(index + 1)
                .ok_or_else(|| format!("missing value for {flag}"))?;
            if found.is_some() {
                return Err(format!("duplicate argument {flag}"));
            }
            found = Some(value.as_str());
            index += 2;
        } else {
            index += 1;
        }
    }
    Ok(found)
}

fn canonical_workspace(value: &str) -> Result<PathBuf, String> {
    let root = Path::new(value)
        .canonicalize()
        .map_err(|error| format!("cannot resolve workspace: {error}"))?;
    if !root.is_dir() {
        return Err("workspace is not a directory".to_string());
    }
    Ok(root)
}

fn read_bounded(path: &Path, limit: usize) -> Result<Vec<u8>, String> {
    let file = fs::File::open(path).map_err(|error| format!("cannot read plan: {error}"))?;
    let mut bytes = Vec::new();
    file.take((limit + 1) as u64)
        .read_to_end(&mut bytes)
        .map_err(|error| format!("cannot read plan: {error}"))?;
    if bytes.len() > limit {
        return Err("plan exceeds the one-megabyte limit".to_string());
    }
    Ok(bytes)
}

fn copy_workspace(source: &Path, destination: &Path) -> Result<(), String> {
    let mut files = 0_usize;
    let mut bytes = 0_u64;
    for entry in WalkDir::new(source)
        .follow_links(false)
        .into_iter()
        .filter_entry(|entry| {
            if entry.depth() == 0 {
                return true;
            }
            let relative = entry.path().strip_prefix(source).unwrap_or(entry.path());
            !entry.file_type().is_symlink()
                && !path_is_sensitive(relative)
                && !path_is_generated(relative)
        })
    {
        let entry = entry.map_err(|error| format!("cannot inventory workspace: {error}"))?;
        if entry.depth() == 0 {
            continue;
        }
        let relative = entry
            .path()
            .strip_prefix(source)
            .map_err(|_| "workspace copy escaped its source".to_string())?;
        let target = destination.join(relative);
        if entry.file_type().is_dir() {
            fs::create_dir_all(&target)
                .map_err(|error| format!("cannot create sandbox directory: {error}"))?;
        } else if entry.file_type().is_file() {
            files += 1;
            bytes = bytes.saturating_add(entry.metadata().map_err(|e| e.to_string())?.len());
            if files > MAX_COPY_FILES || bytes > MAX_COPY_BYTES {
                return Err("workspace exceeds the bounded local sandbox limits".to_string());
            }
            if let Some(parent) = target.parent() {
                fs::create_dir_all(parent).map_err(|error| error.to_string())?;
            }
            fs::copy(entry.path(), &target)
                .map_err(|error| format!("cannot copy workspace file: {error}"))?;
            fs::set_permissions(
                &target,
                entry.metadata().map_err(|e| e.to_string())?.permissions(),
            )
            .map_err(|error| format!("cannot preserve file permissions: {error}"))?;
        }
    }
    Ok(())
}

fn validate_relative_path(value: &str) -> Result<(), String> {
    let path = Path::new(value);
    if path.as_os_str().is_empty()
        || path.is_absolute()
        || path
            .components()
            .any(|component| matches!(component, Component::ParentDir))
        || path_is_sensitive(path)
        || path_is_generated(path)
    {
        return Err(format!("unsafe patch path: {value}"));
    }
    Ok(())
}

fn safe_original_file(root: &Path, relative: &str) -> Result<PathBuf, String> {
    validate_relative_path(relative)?;
    let path = root.join(relative);
    let metadata = fs::symlink_metadata(&path)
        .map_err(|error| format!("cannot inspect patch target {relative}: {error}"))?;
    if !metadata.file_type().is_file() || metadata.file_type().is_symlink() {
        return Err(format!("patch target is not a regular file: {relative}"));
    }
    let canonical = path
        .canonicalize()
        .map_err(|error| format!("cannot resolve patch target {relative}: {error}"))?;
    if !canonical.starts_with(root) {
        return Err(format!("patch target escapes the workspace: {relative}"));
    }
    Ok(canonical)
}

fn collect_changes(
    sandbox: &Path,
    originals: &BTreeMap<String, String>,
) -> Result<Vec<Value>, String> {
    originals
        .iter()
        .map(|(path, before)| {
            let updated = fs::read(sandbox.join(path))
                .map_err(|error| format!("cannot read sandbox result {path}: {error}"))?;
            Ok(json!({
                "path": path,
                "before_sha256": before,
                "after_sha256": sha256_bytes(&updated),
                "changed": sha256_bytes(&updated) != *before,
            }))
        })
        .collect()
}

fn atomic_replace_bytes(bytes: &[u8], destination: &Path) -> Result<(), String> {
    let metadata = fs::metadata(destination)
        .map_err(|error| format!("cannot inspect destination file: {error}"))?;
    let parent = destination
        .parent()
        .ok_or_else(|| "destination has no parent".to_string())?;
    let mut temporary = tempfile::NamedTempFile::new_in(parent)
        .map_err(|error| format!("cannot create atomic output: {error}"))?;
    temporary
        .as_file()
        .set_permissions(metadata.permissions())
        .map_err(|error| format!("cannot preserve destination permissions: {error}"))?;
    temporary
        .write_all(bytes)
        .and_then(|_| temporary.as_file().sync_all())
        .map_err(|error| format!("cannot write atomic output: {error}"))?;
    temporary
        .persist(destination)
        .map_err(|error| format!("cannot replace destination: {}", error.error))?;
    Ok(())
}
