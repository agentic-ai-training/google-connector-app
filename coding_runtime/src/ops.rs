//! Read-only process, log, and runtime observations for trusted orchestration.
//!
//! This broker intentionally has no mutation, shell-text, signal, credential, or network
//! operation. It is a separate authority surface from repository editing.

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::fs;
use std::io::Read;
use std::path::{Component, Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};
use wait_timeout::ChildExt;

const MAX_LOG_BYTES: usize = 262_144;
const MAX_LOG_LINES: usize = 2_000;
const MAX_PROCESSES: usize = 500;

#[derive(Debug, Deserialize)]
#[serde(tag = "tool", rename_all = "snake_case", deny_unknown_fields)]
pub enum OpsRequest {
    ProcessSnapshot {
        #[serde(default)]
        executable_filter: String,
    },
    TailLog {
        path: String,
        #[serde(default = "default_log_lines")]
        lines: usize,
    },
    MigrationInventory {
        #[serde(default = "default_migration_path")]
        path: String,
    },
}

#[derive(Debug, Serialize)]
pub struct OpsResponse {
    pub ok: bool,
    pub tool: String,
    pub duration_ms: u128,
    pub result: Value,
    pub error: Option<Value>,
}

pub struct OpsBroker {
    root: PathBuf,
}

impl OpsBroker {
    pub fn new(root: impl AsRef<Path>) -> Result<Self, String> {
        let root = root
            .as_ref()
            .canonicalize()
            .map_err(|error| error.to_string())?;
        if !root.is_dir() {
            return Err("operations workspace is not a directory".to_string());
        }
        Ok(Self { root })
    }

    pub fn execute(&self, request: OpsRequest) -> OpsResponse {
        let started = Instant::now();
        let tool = match &request {
            OpsRequest::ProcessSnapshot { .. } => "process_snapshot",
            OpsRequest::TailLog { .. } => "tail_log",
            OpsRequest::MigrationInventory { .. } => "migration_inventory",
        };
        let result = match request {
            OpsRequest::ProcessSnapshot { executable_filter } => {
                self.process_snapshot(&executable_filter)
            }
            OpsRequest::TailLog { path, lines } => self.tail_log(&path, lines),
            OpsRequest::MigrationInventory { path } => self.migration_inventory(&path),
        };
        match result {
            Ok(result) => OpsResponse {
                ok: true,
                tool: tool.to_string(),
                duration_ms: started.elapsed().as_millis(),
                result,
                error: None,
            },
            Err(message) => OpsResponse {
                ok: false,
                tool: tool.to_string(),
                duration_ms: started.elapsed().as_millis(),
                result: json!({}),
                error: Some(json!({"code":"ops_denied_or_failed","message":message})),
            },
        }
    }

    fn process_snapshot(&self, filter: &str) -> Result<Value, String> {
        if filter.len() > 120
            || !filter.chars().all(|value| {
                value.is_ascii_alphanumeric() || matches!(value, '-' | '_' | '.' | '/' | ' ')
            })
        {
            return Err("process filter is invalid".to_string());
        }
        let mut child = Command::new("ps")
            .args(["-axo", "pid=,ppid=,etime=,stat=,comm="])
            .env_clear()
            .env("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|error| format!("cannot start process inventory: {error}"))?;
        if child
            .wait_timeout(Duration::from_secs(5))
            .map_err(|error| error.to_string())?
            .is_none()
        {
            let _ = child.kill();
            let _ = child.wait();
            return Err("process inventory timed out".to_string());
        }
        let mut output = String::new();
        child
            .stdout
            .take()
            .ok_or_else(|| "process output unavailable".to_string())?
            .take(MAX_LOG_BYTES as u64)
            .read_to_string(&mut output)
            .map_err(|error| error.to_string())?;
        let needle = filter.trim().to_ascii_lowercase();
        let rows = output
            .lines()
            .filter_map(|line| {
                let fields = line.split_whitespace().collect::<Vec<_>>();
                if fields.len() < 5 {
                    return None;
                }
                let executable = fields[4..].join(" ");
                if !needle.is_empty() && !executable.to_ascii_lowercase().contains(&needle) {
                    return None;
                }
                Some(json!({
                    "pid": fields[0].parse::<u32>().ok(),
                    "parent_pid": fields[1].parse::<u32>().ok(),
                    "elapsed": fields[2], "state": fields[3], "executable": executable,
                }))
            })
            .take(MAX_PROCESSES)
            .collect::<Vec<_>>();
        Ok(
            json!({"processes":rows,"truncated":rows.len()>=MAX_PROCESSES,"arguments_excluded":true}),
        )
    }

    fn tail_log(&self, raw: &str, lines: usize) -> Result<Value, String> {
        if lines == 0 || lines > MAX_LOG_LINES {
            return Err("log line count must be between 1 and 2000".to_string());
        }
        let path = self.safe_file(raw)?;
        let metadata = fs::metadata(&path).map_err(|error| error.to_string())?;
        let mut file = fs::File::open(&path).map_err(|error| error.to_string())?;
        let skip = metadata.len().saturating_sub(MAX_LOG_BYTES as u64);
        if skip > 0 {
            use std::io::{Seek, SeekFrom};
            file.seek(SeekFrom::Start(skip))
                .map_err(|error| error.to_string())?;
        }
        let mut content = String::new();
        file.take((MAX_LOG_BYTES + 1) as u64)
            .read_to_string(&mut content)
            .map_err(|_| "log is not bounded UTF-8 text".to_string())?;
        let selected = content
            .lines()
            .rev()
            .take(lines)
            .collect::<Vec<_>>()
            .into_iter()
            .rev()
            .collect::<Vec<_>>()
            .join("\n");
        Ok(json!({
            "path": path.strip_prefix(&self.root).unwrap_or(&path).to_string_lossy(),
            "content": selected,
            "source_bytes": metadata.len(),
            "tail_truncated": skip > 0 || content.lines().count() > lines,
        }))
    }

    fn migration_inventory(&self, raw: &str) -> Result<Value, String> {
        let path = self.safe_directory(raw)?;
        let mut entries = fs::read_dir(path)
            .map_err(|error| error.to_string())?
            .filter_map(Result::ok)
            .filter(|entry| {
                entry
                    .file_type()
                    .map(|kind| kind.is_file())
                    .unwrap_or(false)
            })
            .filter_map(|entry| {
                let name = entry.file_name().to_string_lossy().to_string();
                if name.ends_with(".py") || name.ends_with(".sql") {
                    Some(name)
                } else {
                    None
                }
            })
            .take(2_000)
            .collect::<Vec<_>>();
        entries.sort();
        Ok(json!({"path":raw,"migration_files":entries,"count":entries.len()}))
    }

    fn safe_file(&self, raw: &str) -> Result<PathBuf, String> {
        let path = self.safe_existing(raw)?;
        if !path.is_file() {
            return Err("log path is not a regular file".to_string());
        }
        Ok(path)
    }

    fn safe_directory(&self, raw: &str) -> Result<PathBuf, String> {
        let path = self.safe_existing(raw)?;
        if !path.is_dir() {
            return Err("migration path is not a directory".to_string());
        }
        Ok(path)
    }

    fn safe_existing(&self, raw: &str) -> Result<PathBuf, String> {
        let path = Path::new(raw.trim());
        if path.as_os_str().is_empty()
            || path.is_absolute()
            || path
                .components()
                .any(|part| matches!(part, Component::ParentDir))
            || crate::path_is_sensitive(path)
            || crate::path_is_generated(path)
        {
            return Err("operations path is denied".to_string());
        }
        let resolved = self
            .root
            .join(path)
            .canonicalize()
            .map_err(|error| error.to_string())?;
        if !resolved.starts_with(&self.root) {
            return Err("operations path escapes workspace".to_string());
        }
        Ok(resolved)
    }
}

fn default_log_lines() -> usize {
    200
}
fn default_migration_path() -> String {
    "migrations/versions".to_string()
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    #[test]
    fn log_tail_is_bounded_and_traversal_is_denied() {
        let root = TempDir::new().unwrap();
        fs::write(root.path().join("server.log"), "one\ntwo\nthree\n").unwrap();
        let broker = OpsBroker::new(root.path()).unwrap();
        let response = broker.execute(OpsRequest::TailLog {
            path: "server.log".into(),
            lines: 2,
        });
        assert!(response.ok);
        assert_eq!(response.result["content"], "two\nthree");
        assert!(
            !broker
                .execute(OpsRequest::TailLog {
                    path: "../secret".into(),
                    lines: 1
                })
                .ok
        );
    }
}
