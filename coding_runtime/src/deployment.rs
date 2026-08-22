//! Read-only deployment inspection with fixed Docker operations.
//!
//! No build, push, login, deploy, scale, restart, or delete operation is registered.

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::path::{Component, Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};
use wait_timeout::ChildExt;

const MAX_OUTPUT: usize = 262_144;

#[derive(Debug, Deserialize)]
#[serde(tag = "tool", rename_all = "snake_case", deny_unknown_fields)]
pub enum DeploymentRequest {
    ComposeValidate {
        #[serde(default = "default_compose_file")]
        path: String,
    },
    ComposeStatus {
        #[serde(default = "default_compose_file")]
        path: String,
    },
    ImageMetadata {
        image: String,
    },
}

#[derive(Debug, Serialize)]
pub struct DeploymentResponse {
    pub ok: bool,
    pub tool: String,
    pub duration_ms: u128,
    pub result: Value,
    pub error: Option<Value>,
}

pub struct DeploymentBroker {
    root: PathBuf,
}

impl DeploymentBroker {
    pub fn new(root: impl AsRef<Path>) -> Result<Self, String> {
        let root = root
            .as_ref()
            .canonicalize()
            .map_err(|error| error.to_string())?;
        if !root.is_dir() {
            return Err("deployment workspace is not a directory".to_string());
        }
        Ok(Self { root })
    }

    pub fn execute(&self, request: DeploymentRequest) -> DeploymentResponse {
        let started = Instant::now();
        let tool = match &request {
            DeploymentRequest::ComposeValidate { .. } => "compose_validate",
            DeploymentRequest::ComposeStatus { .. } => "compose_status",
            DeploymentRequest::ImageMetadata { .. } => "image_metadata",
        };
        let result = match request {
            DeploymentRequest::ComposeValidate { path } => self.compose(&path, true),
            DeploymentRequest::ComposeStatus { path } => self.compose(&path, false),
            DeploymentRequest::ImageMetadata { image } => self.image_metadata(&image),
        };
        match result {
            Ok(result) => DeploymentResponse {
                ok: true,
                tool: tool.into(),
                duration_ms: started.elapsed().as_millis(),
                result,
                error: None,
            },
            Err(message) => DeploymentResponse {
                ok: false,
                tool: tool.into(),
                duration_ms: started.elapsed().as_millis(),
                result: json!({}),
                error: Some(json!({"code":"deployment_read_failed","message":message})),
            },
        }
    }

    fn compose(&self, raw: &str, validate: bool) -> Result<Value, String> {
        let compose = self.safe_compose(raw)?;
        let relative = compose
            .strip_prefix(&self.root)
            .map_err(|_| "compose path escaped".to_string())?;
        let file = relative.to_string_lossy().to_string();
        let args = if validate {
            vec!["compose", "-f", file.as_str(), "config", "--quiet"]
        } else {
            vec!["compose", "-f", file.as_str(), "ps", "--format", "json"]
        };
        self.run_docker(&args, if validate { "validation" } else { "status" })
    }

    fn image_metadata(&self, image: &str) -> Result<Value, String> {
        if image.is_empty()
            || image.len() > 300
            || !image
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || b"-._/:@".contains(&byte))
        {
            return Err("image reference is invalid".to_string());
        }
        self.run_docker(
            &[
                "image",
                "inspect",
                image,
                "--format",
                "{{json .Id}} {{json .RepoDigests}} {{json .Created}} {{json .Config.User}}",
            ],
            "image_metadata",
        )
    }

    fn run_docker(&self, arguments: &[&str], kind: &str) -> Result<Value, String> {
        let mut child = Command::new("docker")
            .args(arguments)
            .current_dir(&self.root)
            .env_clear()
            .env("PATH", "/usr/local/bin:/usr/bin:/bin")
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|_| "Docker CLI is unavailable".to_string())?;
        let status = child
            .wait_timeout(Duration::from_secs(20))
            .map_err(|_| "Docker wait failed".to_string())?;
        if status.is_none() {
            let _ = child.kill();
            let _ = child.wait();
            return Err("Docker read timed out".to_string());
        }
        let output = child
            .wait_with_output()
            .map_err(|_| "Docker output failed".to_string())?;
        let stdout = String::from_utf8_lossy(&output.stdout[..output.stdout.len().min(MAX_OUTPUT)])
            .to_string();
        let stderr = String::from_utf8_lossy(&output.stderr[..output.stderr.len().min(MAX_OUTPUT)])
            .to_string();
        Ok(
            json!({"kind":kind,"success":output.status.success(),"exit_code":output.status.code(),"stdout":stdout,"stderr":stderr,"mutating":false}),
        )
    }

    fn safe_compose(&self, raw: &str) -> Result<PathBuf, String> {
        let path = Path::new(raw.trim());
        if path.as_os_str().is_empty()
            || path.is_absolute()
            || path
                .components()
                .any(|part| matches!(part, Component::ParentDir))
            || crate::path_is_sensitive(path)
            || crate::path_is_generated(path)
        {
            return Err("compose path is denied".to_string());
        }
        let resolved = self
            .root
            .join(path)
            .canonicalize()
            .map_err(|_| "compose file is unavailable".to_string())?;
        if !resolved.starts_with(&self.root) || !resolved.is_file() {
            return Err("compose file is unsafe".to_string());
        }
        let name = resolved
            .file_name()
            .and_then(|value| value.to_str())
            .unwrap_or("");
        if !matches!(
            name,
            "docker-compose.yml" | "docker-compose.yaml" | "compose.yml" | "compose.yaml"
        ) {
            return Err("only a standard Compose file may be inspected".to_string());
        }
        Ok(resolved)
    }
}

fn default_compose_file() -> String {
    "docker-compose.yml".to_string()
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    #[test]
    fn compose_path_policy_rejects_traversal_and_arbitrary_yaml() {
        let root = TempDir::new().unwrap();
        std::fs::write(root.path().join("random.yml"), "value: true\n").unwrap();
        let broker = DeploymentBroker::new(root.path()).unwrap();
        assert!(broker.safe_compose("../compose.yml").is_err());
        assert!(broker.safe_compose("random.yml").is_err());
    }
}
