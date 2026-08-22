use google_connector_coding_runtime::{
    Broker, ToolRequest,
    database::{DatabaseBroker, DatabaseRequest},
    deployment::{DeploymentBroker, DeploymentRequest},
    local_agent,
    ops::{OpsBroker, OpsRequest},
    parse_request, path_is_generated, path_is_sensitive, sha256_bytes,
};
use serde::Deserialize;
use serde_json::{Value, json};
use similar::TextDiff;
use std::collections::BTreeMap;
use std::fs;
use std::io::{Read, Write};
use std::path::{Component, Path, PathBuf};
use std::process::{Command, Stdio};
use tempfile::TempDir;
use walkdir::WalkDir;

const MAX_PLAN_BYTES: usize = 1_048_576;
const MAX_ACTIONS: usize = 50;
const MAX_COPY_FILES: usize = 20_000;
const MAX_COPY_BYTES: u64 = 536_870_912;
const MAX_DIFF_CHARS: usize = 64_000;

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
        "plan-request" => {
            reject_unknown_arguments(
                &arguments,
                &[
                    "--workspace",
                    "--request-file",
                    "--allow-cloud-source",
                    "--model",
                    "--state-dir",
                ],
            )?;
            plan_natural_language_request(&arguments)
        }
        "resume-request" => {
            reject_unknown_arguments(
                &arguments,
                &["--workspace", "--run-id", "--allow-cloud-source", "--state-dir"],
            )?;
            resume_natural_language_request(&arguments)
        }
        "broker-read" => {
            reject_unknown_arguments(&arguments, &["--workspace"])?;
            execute_readonly_broker(&arguments)
        }
        "ops-read" => {
            reject_unknown_arguments(&arguments, &["--workspace"])?;
            execute_operations_broker(&arguments)
        }
        "database-read" => {
            reject_unknown_arguments(&arguments, &["--database-url-env"])?;
            execute_database_broker(&arguments)
        }
        "deployment-read" => {
            reject_unknown_arguments(&arguments, &["--workspace"])?;
            execute_deployment_broker(&arguments)
        }
        _ => Err(
            "usage: gca-local doctor --workspace <path> | gca-local plan-request --workspace <path> --request-file <file> --allow-cloud-source true [--model <groq-model>] [--state-dir <path>] | gca-local resume-request --workspace <path> --run-id <id> --allow-cloud-source true [--state-dir <path>] | gca-local execute-plan --workspace <path> --plan <file> [--approve <plan-sha256>] | gca-local ops-read --workspace <path> | gca-local database-read --database-url-env <name> | gca-local deployment-read --workspace <path>"
                .to_string(),
        ),
    }
}

fn plan_natural_language_request(arguments: &[String]) -> Result<(), String> {
    let workspace = canonical_workspace(required_value(arguments, "--workspace")?)?;
    if required_value(arguments, "--allow-cloud-source")? != "true" {
        return Err(
            "natural-language planning requires --allow-cloud-source true because bounded source excerpts are sent to Groq; use execute-plan for fully offline operation"
                .to_string(),
        );
    }
    let request_path = PathBuf::from(required_value(arguments, "--request-file")?);
    let request_bytes = read_bounded(&request_path, 48_000)?;
    let request = String::from_utf8(request_bytes)
        .map_err(|_| "request file must contain UTF-8 text".to_string())?;
    let model = optional_value(arguments, "--model")?.unwrap_or("llama-3.3-70b-versatile");
    let state_directory = optional_value(arguments, "--state-dir")?
        .map(PathBuf::from)
        .unwrap_or_else(default_state_directory);
    let executable = std::env::current_exe()
        .map_err(|error| format!("cannot resolve local runner executable: {error}"))?;
    let outcome =
        local_agent::plan_request(&workspace, &request, model, &state_directory, &executable)?;
    preview_agent_outcome(&workspace, &executable, outcome)
}

fn resume_natural_language_request(arguments: &[String]) -> Result<(), String> {
    let workspace = canonical_workspace(required_value(arguments, "--workspace")?)?;
    require_cloud_source_consent(arguments)?;
    let run_id = required_value(arguments, "--run-id")?;
    let state_directory = optional_value(arguments, "--state-dir")?
        .map(PathBuf::from)
        .unwrap_or_else(default_state_directory);
    let executable = std::env::current_exe()
        .map_err(|error| format!("cannot resolve local runner executable: {error}"))?;
    let outcome = local_agent::resume_request(&workspace, run_id, &state_directory, &executable)?;
    preview_agent_outcome(&workspace, &executable, outcome)
}

fn require_cloud_source_consent(arguments: &[String]) -> Result<(), String> {
    if required_value(arguments, "--allow-cloud-source")? != "true" {
        return Err(
            "natural-language planning requires --allow-cloud-source true because bounded source excerpts are sent to Groq; use execute-plan for fully offline operation"
                .to_string(),
        );
    }
    Ok(())
}

fn preview_agent_outcome(
    workspace: &Path,
    executable: &Path,
    outcome: local_agent::LocalAgentOutcome,
) -> Result<(), String> {
    let path = std::env::var("PATH").unwrap_or_else(|_| "/usr/local/bin:/usr/bin:/bin".to_string());
    let preview = Command::new(executable)
        .args(["execute-plan", "--workspace"])
        .arg(workspace)
        .arg("--plan")
        .arg(&outcome.plan_path)
        .env_clear()
        .env("PATH", path)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .output()
        .map_err(|error| format!("cannot start keyless sandbox preview: {error}"))?;
    let preview_value: Value = serde_json::from_slice(&preview.stdout)
        .map_err(|_| "keyless sandbox preview returned invalid JSON".to_string())?;
    if !preview.status.success() || preview_value["ok"] != true {
        return Err(format!(
            "generated plan failed sandbox preview: {preview_value}"
        ));
    }
    println!(
        "{}",
        json!({
            "ok": true,
            "status": "awaiting_local_approval",
            "run_id": outcome.run_id,
            "model": outcome.model,
            "input_tokens": outcome.input_tokens,
            "output_tokens": outcome.output_tokens,
            "plan_path": outcome.plan_path,
            "preview": preview_value,
            "coding_key_forwarded_to_broker": false,
        })
    );
    Ok(())
}

fn execute_readonly_broker(arguments: &[String]) -> Result<(), String> {
    let workspace = canonical_workspace(required_value(arguments, "--workspace")?)?;
    let mut input = String::new();
    std::io::stdin()
        .take((MAX_PLAN_BYTES + 1) as u64)
        .read_to_string(&mut input)
        .map_err(|error| format!("cannot read broker request: {error}"))?;
    if input.len() > MAX_PLAN_BYTES {
        return Err("broker request exceeds the one-megabyte limit".to_string());
    }
    let request =
        parse_request(&input).map_err(|_| "broker request violates its schema".to_string())?;
    if !matches!(
        &request,
        ToolRequest::Inventory { .. }
            | ToolRequest::ProjectSummary
            | ToolRequest::FindSymbols { .. }
            | ToolRequest::LanguageInventory { .. }
            | ToolRequest::DependencyInventory { .. }
            | ToolRequest::ComplexityInventory { .. }
            | ToolRequest::ConversionContract { .. }
            | ToolRequest::SearchLiteral { .. }
            | ToolRequest::ReadLines { .. }
            | ToolRequest::HashFile { .. }
    ) {
        return Err("broker-read accepts only read-only investigation tools".to_string());
    }
    let broker = Broker::new(workspace)
        .map_err(|error| format!("cannot initialize read-only broker: {error:?}"))?;
    println!(
        "{}",
        serde_json::to_string(&broker.execute(request))
            .map_err(|error| format!("cannot serialize broker response: {error}"))?
    );
    Ok(())
}

fn execute_operations_broker(arguments: &[String]) -> Result<(), String> {
    let workspace = canonical_workspace(required_value(arguments, "--workspace")?)?;
    let mut input = String::new();
    std::io::stdin()
        .take((MAX_PLAN_BYTES + 1) as u64)
        .read_to_string(&mut input)
        .map_err(|error| format!("cannot read operations request: {error}"))?;
    if input.len() > MAX_PLAN_BYTES {
        return Err("operations request exceeds the one-megabyte limit".to_string());
    }
    let request: OpsRequest = serde_json::from_str(&input)
        .map_err(|_| "operations request violates its typed schema".to_string())?;
    let broker = OpsBroker::new(workspace)?;
    let response = broker.execute(request);
    println!(
        "{}",
        serde_json::to_string(&response)
            .map_err(|error| format!("cannot serialize operations response: {error}"))?
    );
    Ok(())
}

fn execute_database_broker(arguments: &[String]) -> Result<(), String> {
    let variable = required_value(arguments, "--database-url-env")?;
    if variable.is_empty()
        || variable.len() > 120
        || !variable
            .bytes()
            .all(|byte| byte.is_ascii_uppercase() || byte.is_ascii_digit() || byte == b'_')
    {
        return Err("database URL environment-variable name is invalid".to_string());
    }
    let url = std::env::var(variable)
        .map_err(|_| "selected database URL environment variable is unavailable".to_string())?;
    let mut input = String::new();
    std::io::stdin()
        .take((MAX_PLAN_BYTES + 1) as u64)
        .read_to_string(&mut input)
        .map_err(|error| format!("cannot read database request: {error}"))?;
    if input.len() > MAX_PLAN_BYTES {
        return Err("database request exceeds the one-megabyte limit".to_string());
    }
    let request: DatabaseRequest = serde_json::from_str(&input)
        .map_err(|_| "database request violates its typed schema".to_string())?;
    let mut broker = DatabaseBroker::connect(&url)?;
    let response = broker.execute(request);
    println!(
        "{}",
        serde_json::to_string(&response)
            .map_err(|error| format!("cannot serialize database response: {error}"))?
    );
    Ok(())
}

fn execute_deployment_broker(arguments: &[String]) -> Result<(), String> {
    let workspace = canonical_workspace(required_value(arguments, "--workspace")?)?;
    let mut input = String::new();
    std::io::stdin()
        .take((MAX_PLAN_BYTES + 1) as u64)
        .read_to_string(&mut input)
        .map_err(|error| format!("cannot read deployment request: {error}"))?;
    if input.len() > MAX_PLAN_BYTES {
        return Err("deployment request exceeds the one-megabyte limit".to_string());
    }
    let request: DeploymentRequest = serde_json::from_str(&input)
        .map_err(|_| "deployment request violates its typed schema".to_string())?;
    let broker = DeploymentBroker::new(workspace)?;
    let response = broker.execute(request);
    println!(
        "{}",
        serde_json::to_string(&response)
            .map_err(|error| format!("cannot serialize deployment response: {error}"))?
    );
    Ok(())
}

fn default_state_directory() -> PathBuf {
    let home = std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(std::env::temp_dir);
    home.join(".local/state/gca-local")
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
    let last_patch = plan.actions.iter().rposition(|action| {
        matches!(
            action,
            ToolRequest::ApplyExactPatch { .. } | ToolRequest::CreateFile { .. }
        )
    });
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
        match &action {
            ToolRequest::ApplyExactPatch { path, .. } => {
                validate_relative_path(path)?;
                original_hashes.entry(path.clone()).or_insert_with(|| {
                    fs::read(root.join(path))
                        .map(|bytes| sha256_bytes(&bytes))
                        .unwrap_or_default()
                });
            }
            ToolRequest::CreateFile { path, .. } => {
                validate_relative_path(path)?;
                original_hashes.entry(path.clone()).or_default();
            }
            _ => {}
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

    let changes = collect_changes(&root, sandbox.path(), &original_hashes)?;
    let manifest_path = preview_manifest_path(&plan_path);
    if approval.is_none() {
        write_preview_manifest(
            &manifest_path,
            &approval_token,
            "awaiting_local_approval",
            &changes,
            &action_results,
        )?;
        println!(
            "{}",
            json!({
                "ok": true,
                "status": "awaiting_local_approval",
                "approval_token": approval_token,
                "original_workspace_modified": false,
                "changes": changes,
                "actions": action_results,
                "approval_manifest": manifest_path,
                "next_command": format!(
                    "gca-local execute-plan --workspace <path> --plan <file> --approve {approval_token}"
                ),
            })
        );
        return Ok(());
    }

    let mut prepared = Vec::new();
    for (path, expected_hash) in &original_hashes {
        let destination = if expected_hash.is_empty() {
            safe_original_new_file(&root, path)?
        } else {
            safe_original_file(&root, path)?
        };
        let current = if expected_hash.is_empty() {
            if destination.exists() {
                return Err(format!(
                    "{path} was created after planning; no files were written"
                ));
            }
            None
        } else {
            let bytes = fs::read(&destination)
                .map_err(|error| format!("cannot re-read {path}: {error}"))?;
            if sha256_bytes(&bytes) != *expected_hash {
                return Err(format!(
                    "{path} changed after planning; no files were written"
                ));
            }
            Some(bytes)
        };
        let updated = fs::read(sandbox.path().join(path))
            .map_err(|error| format!("cannot read validated sandbox file {path}: {error}"))?;
        prepared.push((path.clone(), destination, current, updated));
    }
    for (applied, (path, destination, preimage, updated)) in prepared.iter().enumerate() {
        let outcome = if preimage.is_some() {
            atomic_replace_bytes(updated, destination)
        } else {
            atomic_create_bytes(updated, destination)
        };
        if let Err(error) = outcome {
            let mut rollback_errors = Vec::new();
            for (rollback_path, rollback_destination, preimage, _) in
                prepared[..applied].iter().rev()
            {
                let rollback = if let Some(preimage) = preimage {
                    atomic_replace_bytes(preimage, rollback_destination)
                } else {
                    fs::remove_file(rollback_destination).map_err(|item| item.to_string())
                };
                if let Err(rollback_error) = rollback {
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
    write_preview_manifest(
        &manifest_path,
        &approval_token,
        "applied",
        &changes,
        &action_results,
    )?;
    println!(
        "{}",
        json!({
            "ok": true,
            "status": "applied",
            "approval_token": approval_token,
            "original_workspace_modified": true,
            "changes": changes,
            "actions": action_results,
            "approval_manifest": manifest_path,
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

fn safe_original_new_file(root: &Path, relative: &str) -> Result<PathBuf, String> {
    validate_relative_path(relative)?;
    let path = root.join(relative);
    let mut cursor = root.to_path_buf();
    let parent = Path::new(relative)
        .parent()
        .unwrap_or_else(|| Path::new("."));
    for component in parent.components() {
        let Component::Normal(name) = component else {
            return Err(format!("unsafe new file path: {relative}"));
        };
        cursor.push(name);
        if cursor.exists() {
            let metadata = fs::symlink_metadata(&cursor)
                .map_err(|error| format!("cannot inspect new file parent: {error}"))?;
            if metadata.file_type().is_symlink() || !metadata.is_dir() {
                return Err(format!("new file parent is unsafe: {relative}"));
            }
        }
    }
    if !path.starts_with(root) {
        return Err(format!("new file escapes the workspace: {relative}"));
    }
    Ok(path)
}

fn collect_changes(
    workspace: &Path,
    sandbox: &Path,
    originals: &BTreeMap<String, String>,
) -> Result<Vec<Value>, String> {
    originals
        .iter()
        .map(|(path, before)| {
            let original = if before.is_empty() {
                Vec::new()
            } else {
                fs::read(workspace.join(path))
                    .map_err(|error| format!("cannot read original result {path}: {error}"))?
            };
            let updated = fs::read(sandbox.join(path))
                .map_err(|error| format!("cannot read sandbox result {path}: {error}"))?;
            Ok(json!({
                "path": path,
                "before_sha256": before,
                "after_sha256": sha256_bytes(&updated),
                "changed": sha256_bytes(&updated) != *before,
                "diff": bounded_unified_diff(path, &original, &updated),
            }))
        })
        .collect()
}

fn bounded_unified_diff(path: &str, before: &[u8], after: &[u8]) -> Value {
    let (Ok(before), Ok(after)) = (std::str::from_utf8(before), std::str::from_utf8(after)) else {
        return json!({"kind": "binary", "text": null, "truncated": false});
    };
    let rendered = TextDiff::from_lines(before, after)
        .unified_diff()
        .context_radius(3)
        .header(&format!("a/{path}"), &format!("b/{path}"))
        .to_string();
    let truncated = rendered.chars().count() > MAX_DIFF_CHARS;
    let text: String = rendered.chars().take(MAX_DIFF_CHARS).collect();
    json!({"kind": "unified", "text": text, "truncated": truncated})
}

fn preview_manifest_path(plan_path: &Path) -> PathBuf {
    let name = plan_path
        .file_name()
        .and_then(|value| value.to_str())
        .unwrap_or("plan.json");
    plan_path.with_file_name(format!("{name}.approval.json"))
}

fn write_preview_manifest(
    path: &Path,
    approval_token: &str,
    status: &str,
    changes: &[Value],
    actions: &[Value],
) -> Result<(), String> {
    let bytes = serde_json::to_vec_pretty(&json!({
        "version": 1,
        "status": status,
        "approval_token": approval_token,
        "changes": changes,
        "validation_and_action_evidence": actions,
    }))
    .map_err(|error| format!("cannot serialize approval manifest: {error}"))?;
    let parent = path
        .parent()
        .ok_or_else(|| "approval manifest path has no parent".to_string())?;
    let mut temporary = tempfile::NamedTempFile::new_in(parent)
        .map_err(|error| format!("cannot create approval manifest: {error}"))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        temporary
            .as_file()
            .set_permissions(fs::Permissions::from_mode(0o600))
            .map_err(|error| format!("cannot protect approval manifest: {error}"))?;
    }
    temporary
        .write_all(&bytes)
        .and_then(|_| temporary.as_file().sync_all())
        .map_err(|error| format!("cannot write approval manifest: {error}"))?;
    temporary
        .persist(path)
        .map_err(|error| format!("cannot persist approval manifest: {}", error.error))?;
    Ok(())
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

fn atomic_create_bytes(bytes: &[u8], destination: &Path) -> Result<(), String> {
    if destination.exists() {
        return Err("destination already exists".to_string());
    }
    let parent = destination
        .parent()
        .ok_or_else(|| "destination has no parent".to_string())?;
    fs::create_dir_all(parent)
        .map_err(|error| format!("cannot create destination directory: {error}"))?;
    let mut temporary = tempfile::NamedTempFile::new_in(parent)
        .map_err(|error| format!("cannot create atomic output: {error}"))?;
    temporary
        .write_all(bytes)
        .and_then(|_| temporary.as_file().sync_all())
        .map_err(|error| format!("cannot write atomic output: {error}"))?;
    temporary
        .persist_noclobber(destination)
        .map_err(|error| format!("cannot create destination: {}", error.error))?;
    Ok(())
}
