use reqwest::blocking::Client;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use crate::ToolRequest;

const GROQ_CHAT_COMPLETIONS_URL: &str = "https://api.groq.com/openai/v1/chat/completions";
const MAX_AGENT_TURNS: usize = 10;
const MAX_TOOL_RESULT_CHARS: usize = 8_000;
const MAX_PROVIDER_ERROR_CHARS: usize = 2_000;
const MAX_CHECKPOINT_BYTES: u64 = 8_388_608;
const MAX_TOTAL_PROVIDER_TOKENS: u64 = 10_000;
const MAX_COMPLETION_TOKENS: u64 = 1_200;

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct LocalAgentCheckpoint {
    version: u8,
    run_id: String,
    status: String,
    workspace: PathBuf,
    request: String,
    model: String,
    cloud_source_consent: bool,
    updated_at_unix_ms: u128,
    next_turn: usize,
    input_tokens: u64,
    output_tokens: u64,
    events: Vec<Value>,
    messages: Vec<Value>,
}

pub struct LocalAgentOutcome {
    pub run_id: String,
    pub plan_path: PathBuf,
    pub model: String,
    pub input_tokens: u64,
    pub output_tokens: u64,
}

pub fn plan_request(
    workspace: &Path,
    request: &str,
    model: &str,
    state_directory: &Path,
    current_executable: &Path,
) -> Result<LocalAgentOutcome, String> {
    let api_key = coding_api_key()?;
    if request.trim().is_empty() || request.chars().count() > 12_000 {
        return Err("request must contain between 1 and 12000 characters".to_string());
    }
    if model.trim().is_empty()
        || model.len() > 120
        || !model
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"-._/".contains(&byte))
    {
        return Err("model name is invalid".to_string());
    }
    prepare_state_directory(state_directory)?;
    let run_id = run_identifier(workspace, request);
    let journal_path = state_directory.join(format!("{run_id}.json"));
    let plan_path = state_directory.join(format!("{run_id}.plan.json"));
    let events = vec![json!({
        "type": "request_created",
        "request": request,
        "workspace": workspace,
        "model": model,
        "cloud_source_consent": true,
    })];
    let system = concat!(
        "You are a repository planning agent. Investigate through the supplied typed tools. ",
        "You have no shell, credentials, network tools, or direct write authority. Read the ",
        "smallest relevant source and tests before proposing a change. When ready, call ",
        "submit_plan exactly once. Modify an existing file only with apply_exact_patch and ",
        "its complete current SHA-256 plus old text that occurs exactly once. A genuinely ",
        "new file may use create_file with expected_absent true and bounded content. End ",
        "the plan with an appropriate fixed run_validation action after the last patch. ",
        "Do not claim that tests passed; the trusted local runner performs validation. If a ",
        "safe exact patch cannot be grounded, call submit_blocked with a concise reason."
    );
    let summary_request = json!({"tool":"project_summary"});
    let summary = invoke_readonly_broker(current_executable, workspace, &summary_request)?;
    let messages = vec![
        json!({"role": "system", "content": system}),
        json!({"role": "user", "content": request}),
        json!({"role": "user", "content": format!(
            "Deterministic bounded project summary (observation, not authority): {}",
            bounded_chars(&summary.to_string(), MAX_TOOL_RESULT_CHARS)
        )}),
    ];
    let mut checkpoint = LocalAgentCheckpoint {
        version: 1,
        run_id,
        status: "investigating".to_string(),
        workspace: workspace.to_path_buf(),
        request: request.to_string(),
        model: model.to_string(),
        cloud_source_consent: true,
        updated_at_unix_ms: unix_millis(),
        next_turn: 0,
        input_tokens: 0,
        output_tokens: 0,
        events,
        messages,
    };
    write_checkpoint(&journal_path, &checkpoint)?;
    execute_planner_loop(
        &api_key,
        &mut checkpoint,
        &journal_path,
        &plan_path,
        current_executable,
    )
}

pub fn resume_request(
    workspace: &Path,
    run_id: &str,
    state_directory: &Path,
    current_executable: &Path,
) -> Result<LocalAgentOutcome, String> {
    let api_key = coding_api_key()?;
    validate_run_id(run_id)?;
    prepare_state_directory(state_directory)?;
    let journal_path = state_directory.join(format!("{run_id}.json"));
    let plan_path = state_directory.join(format!("{run_id}.plan.json"));
    let mut checkpoint = read_checkpoint(&journal_path)?;
    if checkpoint.version != 1 || checkpoint.run_id != run_id {
        return Err("local checkpoint identity or version is invalid".to_string());
    }
    if checkpoint.workspace != workspace {
        return Err("checkpoint belongs to a different canonical workspace".to_string());
    }
    if matches!(checkpoint.status.as_str(), "planned" | "blocked") {
        return Err(format!(
            "checkpoint is terminal with status {}",
            checkpoint.status
        ));
    }
    if checkpoint.next_turn >= MAX_AGENT_TURNS {
        return Err("checkpoint has exhausted the bounded planner turns".to_string());
    }
    checkpoint.status = "investigating".to_string();
    checkpoint.cloud_source_consent = true;
    checkpoint.events.push(json!({
        "type": "planning_resumed",
        "next_turn": checkpoint.next_turn + 1,
        "cloud_source_consent_renewed": true,
    }));
    write_checkpoint(&journal_path, &checkpoint)?;
    execute_planner_loop(
        &api_key,
        &mut checkpoint,
        &journal_path,
        &plan_path,
        current_executable,
    )
}

fn execute_planner_loop(
    api_key: &str,
    checkpoint: &mut LocalAgentCheckpoint,
    journal_path: &Path,
    plan_path: &Path,
    current_executable: &Path,
) -> Result<LocalAgentOutcome, String> {
    let tools = tool_schemas();
    let client = Client::builder()
        .timeout(Duration::from_secs(75))
        .build()
        .map_err(|error| format!("cannot initialize Groq client: {error}"))?;
    for turn in checkpoint.next_turn..MAX_AGENT_TURNS {
        let estimated_prompt_tokens = serde_json::to_string(&checkpoint.messages)
            .map(|value| value.chars().count() as u64 / 4 + 1)
            .unwrap_or(MAX_TOTAL_PROVIDER_TOKENS);
        if checkpoint
            .input_tokens
            .saturating_add(checkpoint.output_tokens)
            .saturating_add(estimated_prompt_tokens)
            .saturating_add(MAX_COMPLETION_TOKENS)
            > MAX_TOTAL_PROVIDER_TOKENS
        {
            checkpoint.status = "token_budget_exhausted".to_string();
            checkpoint.events.push(json!({
                "type":"planning_blocked","reason":"provider_token_budget_preflight",
                "budget":MAX_TOTAL_PROVIDER_TOKENS,
            }));
            write_checkpoint(journal_path, checkpoint)?;
            return Err("planner stopped before exceeding its 10000-token budget".to_string());
        }
        let response = match client
            .post(GROQ_CHAT_COMPLETIONS_URL)
            .bearer_auth(api_key)
            .json(&json!({
                "model": checkpoint.model,
                "messages": checkpoint.messages,
                "tools": tools,
                "tool_choice": "required",
                "parallel_tool_calls": false,
                "temperature": 0.0,
                "max_completion_tokens": MAX_COMPLETION_TOKENS,
            }))
            .send()
        {
            Ok(response) => response,
            Err(error) => {
                checkpoint.status = "provider_failed".to_string();
                checkpoint.events.push(json!({
                    "type": "provider_failed",
                    "detail": bounded_chars(&error.to_string(), MAX_PROVIDER_ERROR_CHARS),
                }));
                write_checkpoint(journal_path, checkpoint)?;
                return Err(format!("Groq request failed: {error}"));
            }
        };
        let status = response.status();
        let payload: Value = response
            .json()
            .map_err(|error| format!("Groq returned invalid JSON: {error}"))?;
        if !status.is_success() {
            let detail = bounded_chars(&payload.to_string(), MAX_PROVIDER_ERROR_CHARS);
            checkpoint.events.push(
                json!({"type": "provider_failed", "status": status.as_u16(), "detail": detail}),
            );
            checkpoint.status = "provider_failed".to_string();
            write_checkpoint(journal_path, checkpoint)?;
            return Err(format!("Groq returned HTTP {}", status.as_u16()));
        }
        checkpoint.input_tokens = checkpoint.input_tokens.saturating_add(
            payload["usage"]["prompt_tokens"]
                .as_u64()
                .unwrap_or_default(),
        );
        checkpoint.output_tokens = checkpoint.output_tokens.saturating_add(
            payload["usage"]["completion_tokens"]
                .as_u64()
                .unwrap_or_default(),
        );
        if checkpoint
            .input_tokens
            .saturating_add(checkpoint.output_tokens)
            > MAX_TOTAL_PROVIDER_TOKENS
        {
            checkpoint.status = "token_budget_exhausted".to_string();
            checkpoint.events.push(json!({
                "type":"planning_blocked","reason":"provider_reported_token_budget",
                "budget":MAX_TOTAL_PROVIDER_TOKENS,
            }));
            write_checkpoint(journal_path, checkpoint)?;
            return Err("provider usage exceeded the bounded planner token budget".to_string());
        }
        let message = payload["choices"][0]["message"].clone();
        let calls = message["tool_calls"]
            .as_array()
            .ok_or_else(|| "Groq did not return the required typed tool call".to_string())?
            .clone();
        if calls.is_empty() {
            return Err("Groq returned an empty tool-call list".to_string());
        }
        if calls.len() != 1 {
            return Err("Groq must return exactly one sequential tool call per turn".to_string());
        }
        checkpoint.messages.push(message);

        for call in &calls {
            let call_id = call["id"]
                .as_str()
                .ok_or_else(|| "Groq tool call omitted its ID".to_string())?;
            let name = call["function"]["name"]
                .as_str()
                .ok_or_else(|| "Groq tool call omitted its name".to_string())?;
            let arguments: Value = serde_json::from_str(
                call["function"]["arguments"]
                    .as_str()
                    .ok_or_else(|| "Groq tool call arguments are invalid".to_string())?,
            )
            .map_err(|error| format!("Groq tool arguments are not JSON: {error}"))?;
            checkpoint
                .events
                .push(json!({"type": "tool_requested", "turn": turn + 1, "tool": name}));

            if name == "submit_plan" {
                validate_submitted_plan(&arguments)?;
                let plan_bytes = serde_json::to_vec_pretty(&arguments)
                    .map_err(|error| format!("cannot serialize plan: {error}"))?;
                atomic_private_write(plan_path, &plan_bytes)?;
                checkpoint.events.push(json!({
                    "type": "plan_frozen",
                    "plan_path": plan_path,
                    "plan_sha256": format!("{:x}", Sha256::digest(&plan_bytes)),
                    "input_tokens": checkpoint.input_tokens,
                    "output_tokens": checkpoint.output_tokens,
                }));
                checkpoint.status = "planned".to_string();
                checkpoint.next_turn = turn + 1;
                write_checkpoint(journal_path, checkpoint)?;
                return Ok(LocalAgentOutcome {
                    run_id: checkpoint.run_id.clone(),
                    plan_path: plan_path.to_path_buf(),
                    model: checkpoint.model.clone(),
                    input_tokens: checkpoint.input_tokens,
                    output_tokens: checkpoint.output_tokens,
                });
            }
            if name == "submit_blocked" {
                let reason = arguments["reason"]
                    .as_str()
                    .unwrap_or("no safe plan was produced");
                checkpoint.events.push(
                    json!({"type": "planning_blocked", "reason": bounded_chars(reason, 1000)}),
                );
                checkpoint.status = "blocked".to_string();
                checkpoint.next_turn = turn + 1;
                write_checkpoint(journal_path, checkpoint)?;
                return Err(format!(
                    "planning stopped safely: {}",
                    bounded_chars(reason, 1000)
                ));
            }
            let broker_request = broker_request(name, &arguments)?;
            let broker_response =
                invoke_readonly_broker(current_executable, &checkpoint.workspace, &broker_request)?;
            let bounded = bounded_chars(&broker_response.to_string(), MAX_TOOL_RESULT_CHARS);
            checkpoint.events.push(json!({
                "type": "tool_completed",
                "turn": turn + 1,
                "tool": name,
                "result_sha256": format!("{:x}", Sha256::digest(bounded.as_bytes())),
            }));
            checkpoint
                .messages
                .push(json!({"role": "tool", "tool_call_id": call_id, "content": bounded}));
            checkpoint.next_turn = turn + 1;
            write_checkpoint(journal_path, checkpoint)?;
        }
    }
    checkpoint
        .events
        .push(json!({"type": "planning_failed", "reason": "turn_limit"}));
    checkpoint.status = "turn_limit".to_string();
    write_checkpoint(journal_path, checkpoint)?;
    Err(format!(
        "planner exceeded {MAX_AGENT_TURNS} bounded tool turns"
    ))
}

fn coding_api_key() -> Result<String, String> {
    let api_key = std::env::var("CODING_GROQ_API_KEY")
        .map_err(|_| "CODING_GROQ_API_KEY is required for natural-language planning")?;
    if api_key.trim().is_empty() {
        return Err("CODING_GROQ_API_KEY is empty".to_string());
    }
    Ok(api_key)
}

fn broker_request(name: &str, arguments: &Value) -> Result<Value, String> {
    let allowed = [
        "inventory",
        "project_summary",
        "find_symbols",
        "language_inventory",
        "dependency_inventory",
        "complexity_inventory",
        "conversion_contract",
        "search_literal",
        "read_lines",
        "hash_file",
    ];
    if !allowed.contains(&name) {
        return Err(format!(
            "model requested an unavailable investigation tool: {name}"
        ));
    }
    let mut object = arguments
        .as_object()
        .cloned()
        .ok_or_else(|| "tool arguments must be an object".to_string())?;
    object.insert("tool".to_string(), Value::String(name.to_string()));
    Ok(Value::Object(object))
}

fn invoke_readonly_broker(
    executable: &Path,
    workspace: &Path,
    request: &Value,
) -> Result<Value, String> {
    let mut child = Command::new(executable)
        .args(["broker-read", "--workspace"])
        .arg(workspace)
        .env_clear()
        .env(
            "PATH",
            std::env::var("PATH").unwrap_or_else(|_| "/usr/local/bin:/usr/bin:/bin".to_string()),
        )
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|error| format!("cannot start read-only broker: {error}"))?;
    child
        .stdin
        .take()
        .ok_or_else(|| "read-only broker stdin is unavailable".to_string())?
        .write_all(request.to_string().as_bytes())
        .map_err(|error| format!("cannot send broker request: {error}"))?;
    let output = child
        .wait_with_output()
        .map_err(|error| format!("cannot wait for read-only broker: {error}"))?;
    if !output.status.success() {
        return Err("read-only broker rejected the investigation request".to_string());
    }
    serde_json::from_slice(&output.stdout)
        .map_err(|error| format!("read-only broker returned invalid JSON: {error}"))
}

fn validate_submitted_plan(plan: &Value) -> Result<(), String> {
    let actions = plan["actions"]
        .as_array()
        .ok_or_else(|| "submitted plan must contain an actions array".to_string())?;
    if actions.is_empty() || actions.len() > 50 {
        return Err("submitted plan must contain between 1 and 50 actions".to_string());
    }
    let mut last_patch = None;
    let mut last_validation = None;
    for (index, action) in actions.iter().enumerate() {
        let typed: ToolRequest = serde_json::from_value(action.clone())
            .map_err(|_| format!("plan action {} violates the broker schema", index + 1))?;
        match typed {
            ToolRequest::Inventory { .. }
            | ToolRequest::ProjectSummary
            | ToolRequest::FindSymbols { .. }
            | ToolRequest::LanguageInventory { .. }
            | ToolRequest::DependencyInventory { .. }
            | ToolRequest::ComplexityInventory { .. }
            | ToolRequest::ConversionContract { .. }
            | ToolRequest::SearchLiteral { .. }
            | ToolRequest::ReadLines { .. }
            | ToolRequest::HashFile { .. } => {}
            ToolRequest::ApplyExactPatch { .. } | ToolRequest::CreateFile { .. } => {
                last_patch = Some(index)
            }
            ToolRequest::RunValidation { .. } => last_validation = Some(index),
            ToolRequest::GitStatus | ToolRequest::GitDiff { .. } => {
                return Err(
                    "natural-language plans cannot acquire Git command authority".to_string(),
                );
            }
        }
    }
    let patch = last_patch.ok_or_else(|| "submitted plan contains no exact patch".to_string())?;
    if !last_validation.is_some_and(|validation| validation > patch) {
        return Err("submitted plan must validate after its final patch".to_string());
    }
    Ok(())
}

fn tool_schemas() -> Vec<Value> {
    vec![
        function_tool(
            "inventory",
            json!({"type":"object","additionalProperties":false,"properties":{"path":{"type":"string"},"max_depth":{"type":"integer","minimum":1,"maximum":20}},"required":["path","max_depth"]}),
        ),
        function_tool(
            "project_summary",
            json!({"type":"object","additionalProperties":false,"properties":{},"required":[]}),
        ),
        function_tool(
            "find_symbols",
            json!({"type":"object","additionalProperties":false,"properties":{"query":{"type":"string"},"paths":{"type":"array","items":{"type":"string"}}},"required":["query","paths"]}),
        ),
        function_tool(
            "language_inventory",
            json!({"type":"object","additionalProperties":false,"properties":{"paths":{"type":"array","items":{"type":"string"}}},"required":["paths"]}),
        ),
        function_tool(
            "dependency_inventory",
            json!({"type":"object","additionalProperties":false,"properties":{"paths":{"type":"array","items":{"type":"string"}}},"required":["paths"]}),
        ),
        function_tool(
            "complexity_inventory",
            json!({"type":"object","additionalProperties":false,"properties":{"paths":{"type":"array","items":{"type":"string"}}},"required":["paths"]}),
        ),
        function_tool(
            "conversion_contract",
            json!({"type":"object","additionalProperties":false,"properties":{"source_path":{"type":"string"},"target_language":{"type":"string"}},"required":["source_path","target_language"]}),
        ),
        function_tool(
            "search_literal",
            json!({"type":"object","additionalProperties":false,"properties":{"query":{"type":"string"},"paths":{"type":"array","items":{"type":"string"}},"case_sensitive":{"type":"boolean"}},"required":["query","paths","case_sensitive"]}),
        ),
        function_tool(
            "read_lines",
            json!({"type":"object","additionalProperties":false,"properties":{"path":{"type":"string"},"start_line":{"type":"integer","minimum":1},"end_line":{"type":"integer","minimum":1}},"required":["path","start_line","end_line"]}),
        ),
        function_tool(
            "hash_file",
            json!({"type":"object","additionalProperties":false,"properties":{"path":{"type":"string"}},"required":["path"]}),
        ),
        function_tool(
            "submit_plan",
            json!({"type":"object","additionalProperties":false,"properties":{"actions":{"type":"array","minItems":1,"maxItems":50,"items":{"type":"object"}}},"required":["actions"]}),
        ),
        function_tool(
            "submit_blocked",
            json!({"type":"object","additionalProperties":false,"properties":{"reason":{"type":"string"}},"required":["reason"]}),
        ),
    ]
}

fn function_tool(name: &str, parameters: Value) -> Value {
    json!({"type": "function", "function": {"name": name, "parameters": parameters}})
}

fn prepare_state_directory(path: &Path) -> Result<(), String> {
    fs::create_dir_all(path).map_err(|error| format!("cannot create state directory: {error}"))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o700))
            .map_err(|error| format!("cannot protect state directory: {error}"))?;
    }
    Ok(())
}

fn write_checkpoint(path: &Path, checkpoint: &LocalAgentCheckpoint) -> Result<(), String> {
    let mut checkpoint = LocalAgentCheckpoint {
        version: checkpoint.version,
        run_id: checkpoint.run_id.clone(),
        status: checkpoint.status.clone(),
        workspace: checkpoint.workspace.clone(),
        request: checkpoint.request.clone(),
        model: checkpoint.model.clone(),
        cloud_source_consent: checkpoint.cloud_source_consent,
        updated_at_unix_ms: unix_millis(),
        next_turn: checkpoint.next_turn,
        input_tokens: checkpoint.input_tokens,
        output_tokens: checkpoint.output_tokens,
        events: checkpoint.events.clone(),
        messages: checkpoint.messages.clone(),
    };
    checkpoint.updated_at_unix_ms = unix_millis();
    let bytes = serde_json::to_vec_pretty(&checkpoint)
        .map_err(|error| format!("cannot serialize local checkpoint: {error}"))?;
    if bytes.len() as u64 > MAX_CHECKPOINT_BYTES {
        return Err("local checkpoint exceeds its eight-megabyte bound".to_string());
    }
    atomic_private_write(path, &bytes)
}

fn read_checkpoint(path: &Path) -> Result<LocalAgentCheckpoint, String> {
    let metadata =
        fs::metadata(path).map_err(|error| format!("cannot inspect local checkpoint: {error}"))?;
    if !metadata.is_file() || metadata.len() > MAX_CHECKPOINT_BYTES {
        return Err("local checkpoint is unavailable or exceeds its size bound".to_string());
    }
    let bytes = fs::read(path).map_err(|error| format!("cannot read local checkpoint: {error}"))?;
    serde_json::from_slice(&bytes)
        .map_err(|error| format!("local checkpoint violates its schema: {error}"))
}

fn validate_run_id(run_id: &str) -> Result<(), String> {
    if run_id.len() != 26
        || !run_id.starts_with("local-")
        || !run_id[6..].bytes().all(|byte| byte.is_ascii_hexdigit())
    {
        return Err("local run ID is invalid".to_string());
    }
    Ok(())
}

fn atomic_private_write(path: &Path, bytes: &[u8]) -> Result<(), String> {
    let parent = path
        .parent()
        .ok_or_else(|| "state path has no parent".to_string())?;
    let mut temporary = tempfile::NamedTempFile::new_in(parent)
        .map_err(|error| format!("cannot create state file: {error}"))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        temporary
            .as_file()
            .set_permissions(fs::Permissions::from_mode(0o600))
            .map_err(|error| format!("cannot protect state file: {error}"))?;
    }
    temporary
        .write_all(bytes)
        .and_then(|_| temporary.as_file().sync_all())
        .map_err(|error| format!("cannot write state file: {error}"))?;
    temporary
        .persist(path)
        .map_err(|error| format!("cannot persist state file: {}", error.error))?;
    Ok(())
}

fn run_identifier(workspace: &Path, request: &str) -> String {
    let mut hasher = Sha256::new();
    hasher.update(workspace.to_string_lossy().as_bytes());
    hasher.update(request.as_bytes());
    hasher.update(unix_millis().to_le_bytes());
    hasher.update(std::process::id().to_le_bytes());
    format!("local-{}", &format!("{:x}", hasher.finalize())[..20])
}

fn unix_millis() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
}

fn bounded_chars(value: &str, maximum: usize) -> String {
    value.chars().take(maximum).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn submitted_plan_requires_patch_then_validation() {
        let valid = json!({"actions": [
            {
                "tool": "apply_exact_patch",
                "path": "app.py",
                "expected_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "old": "return 1",
                "replacement": "return 2"
            },
            {"tool": "run_validation", "profile": "python_compile", "timeout_seconds": 30}
        ]});
        assert!(validate_submitted_plan(&valid).is_ok());
        let invalid = json!({"actions": [{
            "tool": "apply_exact_patch",
            "path": "app.py",
            "expected_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "old": "return 1",
            "replacement": "return 2"
        }]});
        assert!(validate_submitted_plan(&invalid).is_err());
    }

    #[test]
    fn broker_requests_are_read_only() {
        assert!(
            broker_request(
                "read_lines",
                &json!({"path":"app.py","start_line":1,"end_line":2})
            )
            .is_ok()
        );
        assert!(broker_request("apply_exact_patch", &json!({})).is_err());
        assert!(broker_request("run_validation", &json!({})).is_err());
    }

    #[test]
    fn model_registry_exposes_analysis_without_new_authority() {
        for (name, arguments) in [
            ("language_inventory", json!({"paths":["app"]})),
            ("dependency_inventory", json!({"paths":["app"]})),
            ("complexity_inventory", json!({"paths":["app"]})),
            (
                "conversion_contract",
                json!({"source_path":"app/main.py","target_language":"rust"}),
            ),
        ] {
            assert!(broker_request(name, &arguments).is_ok(), "missing {name}");
        }
        let names = tool_schemas()
            .into_iter()
            .filter_map(|tool| tool["function"]["name"].as_str().map(str::to_string))
            .collect::<Vec<_>>();
        for expected in [
            "language_inventory",
            "dependency_inventory",
            "complexity_inventory",
            "conversion_contract",
        ] {
            assert!(names.contains(&expected.to_string()), "missing {expected}");
        }
        assert!(!names.contains(&"shell".to_string()));
        assert!(!names.contains(&"execute_sql".to_string()));
        assert!(!names.contains(&"deploy".to_string()));
    }
}
