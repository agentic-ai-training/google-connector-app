use google_connector_coding_runtime::sha256_bytes;
use serde_json::{Value, json};
use std::fs;
use std::process::Command;
use tempfile::TempDir;

fn run(arguments: &[&str]) -> std::process::Output {
    Command::new(env!("CARGO_BIN_EXE_gca-local"))
        .args(arguments)
        .output()
        .unwrap()
}

#[test]
fn non_git_workspace_is_previewed_then_applied_with_exact_approval() {
    let workspace = TempDir::new().unwrap();
    let plan_directory = TempDir::new().unwrap();
    fs::create_dir(workspace.path().join("app")).unwrap();
    let source_path = workspace.path().join("app/main.py");
    let original = b"def answer():\n    return 41\n";
    fs::write(&source_path, original).unwrap();
    fs::write(workspace.path().join(".env"), "SECRET=not-copied\n").unwrap();
    let plan = json!({
        "actions": [
            {
                "tool": "read_lines",
                "path": "app/main.py",
                "start_line": 1,
                "end_line": 3
            },
            {
                "tool": "apply_exact_patch",
                "path": "app/main.py",
                "expected_sha256": sha256_bytes(original),
                "old": "return 41",
                "replacement": "return 42"
            },
            {
                "tool": "run_validation",
                "profile": "python_compile",
                "timeout_seconds": 30
            }
        ]
    });
    let plan_bytes = serde_json::to_vec(&plan).unwrap();
    let plan_path = plan_directory.path().join("plan.json");
    fs::write(&plan_path, &plan_bytes).unwrap();

    let preview = Command::new(env!("CARGO_BIN_EXE_gca-local"))
        .args([
            "execute-plan",
            "--workspace",
            workspace.path().to_str().unwrap(),
            "--plan",
            plan_path.to_str().unwrap(),
        ])
        .output()
        .unwrap();
    assert!(
        preview.status.success(),
        "stdout={} stderr={}",
        String::from_utf8_lossy(&preview.stdout),
        String::from_utf8_lossy(&preview.stderr)
    );
    let preview: Value = serde_json::from_slice(&preview.stdout).unwrap();
    assert_eq!(preview["status"], "awaiting_local_approval");
    assert_eq!(preview["original_workspace_modified"], false);
    assert!(
        preview["changes"][0]["diff"]["text"]
            .as_str()
            .unwrap()
            .contains("-    return 41")
    );
    let manifest_path = preview["approval_manifest"].as_str().unwrap();
    let manifest: Value = serde_json::from_slice(&fs::read(manifest_path).unwrap()).unwrap();
    assert_eq!(manifest["status"], "awaiting_local_approval");
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(
            fs::metadata(manifest_path).unwrap().permissions().mode() & 0o777,
            0o600
        );
    }
    assert_eq!(fs::read(&source_path).unwrap(), original);

    let token = preview["approval_token"].as_str().unwrap();
    let applied = Command::new(env!("CARGO_BIN_EXE_gca-local"))
        .args([
            "execute-plan",
            "--workspace",
            workspace.path().to_str().unwrap(),
            "--plan",
            plan_path.to_str().unwrap(),
            "--approve",
            token,
        ])
        .output()
        .unwrap();
    assert!(applied.status.success());
    let applied: Value = serde_json::from_slice(&applied.stdout).unwrap();
    assert_eq!(applied["status"], "applied");
    let applied_manifest: Value =
        serde_json::from_slice(&fs::read(manifest_path).unwrap()).unwrap();
    assert_eq!(applied_manifest["status"], "applied");
    assert!(
        fs::read_to_string(source_path)
            .unwrap()
            .contains("return 42")
    );
    assert_eq!(
        fs::read_to_string(workspace.path().join(".env")).unwrap(),
        "SECRET=not-copied\n"
    );
}

#[test]
fn non_git_workspace_can_preview_and_atomically_create_a_new_file() {
    let workspace = TempDir::new().unwrap();
    fs::write(workspace.path().join("existing.py"), "VALUE = 1\n").unwrap();
    let plan_dir = TempDir::new().unwrap();
    let plan = plan_dir.path().join("create.json");
    fs::write(
        &plan,
        serde_json::to_vec_pretty(&json!({"actions":[
            {"tool":"create_file","path":"tests/test_value.py","expected_absent":true,
             "content":"def test_value():\n    assert 1 == 1\n"},
            {"tool":"run_validation","profile":"python_compile","timeout_seconds":30}
        ]}))
        .unwrap(),
    )
    .unwrap();
    let preview = run(&[
        "execute-plan",
        "--workspace",
        workspace.path().to_str().unwrap(),
        "--plan",
        plan.to_str().unwrap(),
    ]);
    assert!(
        preview.status.success(),
        "{}",
        String::from_utf8_lossy(&preview.stdout)
    );
    assert!(!workspace.path().join("tests/test_value.py").exists());
    let payload: Value = serde_json::from_slice(&preview.stdout).unwrap();
    let approval = payload["approval_token"].as_str().unwrap();
    let applied = run(&[
        "execute-plan",
        "--workspace",
        workspace.path().to_str().unwrap(),
        "--plan",
        plan.to_str().unwrap(),
        "--approve",
        approval,
    ]);
    assert!(
        applied.status.success(),
        "{}",
        String::from_utf8_lossy(&applied.stdout)
    );
    assert!(workspace.path().join("tests/test_value.py").is_file());
}

#[test]
fn doctor_accepts_a_private_non_git_directory() {
    let workspace = TempDir::new().unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_gca-local"))
        .args(["doctor", "--workspace", workspace.path().to_str().unwrap()])
        .output()
        .unwrap();
    assert!(output.status.success());
    let response: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(response["git_repository"], false);
    assert_eq!(response["writes_require_approval_token"], true);
}

#[test]
fn unknown_cli_authority_is_rejected() {
    let workspace = TempDir::new().unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_gca-local"))
        .args([
            "doctor",
            "--workspace",
            workspace.path().to_str().unwrap(),
            "--shell",
            "rm",
        ])
        .output()
        .unwrap();
    assert!(!output.status.success());
    let response: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(response["ok"], false);
    assert!(
        response["error"]
            .as_str()
            .unwrap()
            .contains("unknown argument")
    );
}

#[test]
fn natural_language_mode_requires_explicit_cloud_source_consent() {
    let workspace = TempDir::new().unwrap();
    let request_directory = TempDir::new().unwrap();
    let request = request_directory.path().join("request.txt");
    fs::write(&request, "change the answer safely").unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_gca-local"))
        .args([
            "plan-request",
            "--workspace",
            workspace.path().to_str().unwrap(),
            "--request-file",
            request.to_str().unwrap(),
            "--allow-cloud-source",
            "false",
        ])
        .env_remove("CODING_GROQ_API_KEY")
        .output()
        .unwrap();
    assert!(!output.status.success());
    let response: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert!(
        response["error"]
            .as_str()
            .unwrap()
            .contains("requires --allow-cloud-source true")
    );
}

#[test]
fn readonly_broker_command_rejects_mutation_tools() {
    let workspace = TempDir::new().unwrap();
    fs::write(workspace.path().join("main.py"), "value = 1\n").unwrap();
    let mut child = Command::new(env!("CARGO_BIN_EXE_gca-local"))
        .args([
            "broker-read",
            "--workspace",
            workspace.path().to_str().unwrap(),
        ])
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .spawn()
        .unwrap();
    use std::io::Write;
    child
        .stdin
        .take()
        .unwrap()
        .write_all(
            json!({
                "tool": "apply_exact_patch",
                "path": "main.py",
                "expected_sha256": sha256_bytes(b"value = 1\n"),
                "old": "1",
                "replacement": "2"
            })
            .to_string()
            .as_bytes(),
        )
        .unwrap();
    let output = child.wait_with_output().unwrap();
    assert!(!output.status.success());
    assert_eq!(
        fs::read_to_string(workspace.path().join("main.py")).unwrap(),
        "value = 1\n"
    );
}

#[test]
fn resume_rejects_a_path_like_run_identifier_before_network_access() {
    let workspace = TempDir::new().unwrap();
    let state = TempDir::new().unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_gca-local"))
        .args([
            "resume-request",
            "--workspace",
            workspace.path().to_str().unwrap(),
            "--run-id",
            "../../stolen",
            "--allow-cloud-source",
            "true",
            "--state-dir",
            state.path().to_str().unwrap(),
        ])
        .env("CODING_GROQ_API_KEY", "test-only")
        .output()
        .unwrap();
    assert!(!output.status.success());
    let response: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert!(
        response["error"]
            .as_str()
            .unwrap()
            .contains("run ID is invalid")
    );
}
