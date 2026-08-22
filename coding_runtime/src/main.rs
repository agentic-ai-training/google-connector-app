use google_connector_coding_runtime::{Broker, parse_request};
use serde_json::json;
use std::io::{self, Read};
use std::path::PathBuf;

fn main() {
    let mut arguments = std::env::args().skip(1);
    let root = match (arguments.next().as_deref(), arguments.next()) {
        (Some("--root"), Some(value)) if arguments.next().is_none() => PathBuf::from(value),
        _ => {
            emit_error("usage_error", "usage: coding-runtime --root <workspace>");
            std::process::exit(2);
        }
    };
    let broker = match Broker::new(root) {
        Ok(value) => value,
        Err(_) => {
            emit_error("invalid_root", "workspace root is unavailable");
            std::process::exit(2);
        }
    };
    let mut input = String::new();
    if io::stdin()
        .take(1_048_577)
        .read_to_string(&mut input)
        .is_err()
        || input.len() > 1_048_576
    {
        emit_error(
            "input_limit",
            "request exceeds the one-megabyte protocol limit",
        );
        std::process::exit(2);
    }
    let request = match parse_request(&input) {
        Ok(value) => value,
        Err(_) => {
            emit_error(
                "invalid_request",
                "request is not valid against the typed tool schema",
            );
            std::process::exit(2);
        }
    };
    println!(
        "{}",
        serde_json::to_string(&broker.execute(request)).expect("response serializes")
    );
}

fn emit_error(code: &str, message: &str) {
    println!(
        "{}",
        json!({"ok": false, "tool": "protocol", "duration_ms": 0, "result": {}, "error": {"code": code, "message": message}})
    );
}
