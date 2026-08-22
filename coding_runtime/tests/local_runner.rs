use google_connector_coding_runtime::sha256_bytes;
use serde_json::{Value, json};
use std::fs;
use std::process::Command;
use tempfile::TempDir;

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
