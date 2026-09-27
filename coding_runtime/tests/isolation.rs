use serde_json::{Value, json};
use std::io::Write;
use std::process::{Command, Stdio};
use tempfile::TempDir;

fn validate(workspace: &std::path::Path, launcher: Option<&std::path::Path>) -> Value {
    let mut command = Command::new(env!("CARGO_BIN_EXE_google-connector-coding-runtime"));
    command
        .args(["--root", workspace.to_str().unwrap()])
        .env("GCA_REQUIRE_ISOLATED_VALIDATION", "1")
        .env_remove("GCA_VALIDATION_LAUNCHER")
        .env_remove("GCA_VALIDATION_CONFIG")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped());
    if let Some(path) = launcher {
        command
            .env("GCA_VALIDATION_LAUNCHER", path)
            .env("GCA_VALIDATION_CONFIG", path);
    }
    let mut child = command.spawn().unwrap();
    child
        .stdin
        .take()
        .unwrap()
        .write_all(
            json!({
                "tool": "run_validation", "profile": "python_compile", "timeout_seconds": 5
            })
            .to_string()
            .as_bytes(),
        )
        .unwrap();
    let output = child.wait_with_output().unwrap();
    serde_json::from_slice(&output.stdout).unwrap()
}

#[test]
fn required_isolation_never_falls_back_to_host() {
    let root = TempDir::new().unwrap();
    let result = validate(root.path(), None);
    assert_eq!(result["ok"], false);
    assert_eq!(result["error"]["code"], "isolation_unavailable");
}

#[test]
fn repository_cannot_supply_its_own_trusted_launcher() {
    let root = TempDir::new().unwrap();
    let launcher = root.path().join("launcher");
    std::fs::write(&launcher, "not a trusted launcher").unwrap();
    let result = validate(root.path(), Some(&launcher));
    assert_eq!(result["ok"], false);
    assert_eq!(result["error"]["code"], "isolation_denied");
}
