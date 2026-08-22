//! Schema-only PostgreSQL inspection behind a dedicated credential boundary.
//!
//! The connection string is read from a caller-selected environment variable and is never
//! serialized. Requests contain no SQL. Every transaction is read-only and every result is
//! metadata from `pg_catalog`/`information_schema`, so an LLM cannot turn this into a data-
//! extraction or mutation channel.

use native_tls::TlsConnector;
use postgres::{Client, NoTls};
use postgres_native_tls::MakeTlsConnector;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::time::Instant;

const MAX_SCHEMA_ROWS: usize = 5_000;

#[derive(Debug, Deserialize)]
#[serde(tag = "tool", rename_all = "snake_case", deny_unknown_fields)]
pub enum DatabaseRequest {
    ServerInfo,
    ListSchemas,
    SchemaSnapshot {
        #[serde(default = "default_schema")]
        schema: String,
    },
    TableSchema {
        #[serde(default = "default_schema")]
        schema: String,
        table: String,
    },
    ExtensionInventory,
}

#[derive(Debug, Serialize)]
pub struct DatabaseResponse {
    pub ok: bool,
    pub tool: String,
    pub duration_ms: u128,
    pub result: Value,
    pub error: Option<Value>,
}

pub struct DatabaseBroker {
    client: Client,
}

impl DatabaseBroker {
    pub fn connect(url: &str) -> Result<Self, String> {
        let parsed = url::Url::parse(url).map_err(|_| "database URL is invalid".to_string())?;
        let local = matches!(parsed.host_str(), Some("localhost" | "127.0.0.1" | "::1"));
        let requires_tls = parsed.query_pairs().any(|(key, value)| {
            key == "sslmode" && matches!(value.as_ref(), "require" | "verify-ca" | "verify-full")
        });
        let disables_tls = parsed
            .query_pairs()
            .any(|(key, value)| key == "sslmode" && value == "disable");
        let client = if disables_tls || (local && !requires_tls) {
            Client::connect(url, NoTls).map_err(|_| "database connection failed".to_string())?
        } else {
            let connector = TlsConnector::builder()
                .build()
                .map_err(|_| "database TLS initialization failed".to_string())?;
            Client::connect(url, MakeTlsConnector::new(connector))
                .map_err(|_| "database TLS connection failed".to_string())?
        };
        Ok(Self { client })
    }

    pub fn execute(&mut self, request: DatabaseRequest) -> DatabaseResponse {
        let started = Instant::now();
        let tool = match &request {
            DatabaseRequest::ServerInfo => "server_info",
            DatabaseRequest::ListSchemas => "list_schemas",
            DatabaseRequest::SchemaSnapshot { .. } => "schema_snapshot",
            DatabaseRequest::TableSchema { .. } => "table_schema",
            DatabaseRequest::ExtensionInventory => "extension_inventory",
        };
        let result = self.execute_read_only(request);
        match result {
            Ok(result) => DatabaseResponse {
                ok: true,
                tool: tool.to_string(),
                duration_ms: started.elapsed().as_millis(),
                result,
                error: None,
            },
            Err(message) => DatabaseResponse {
                ok: false,
                tool: tool.to_string(),
                duration_ms: started.elapsed().as_millis(),
                result: json!({}),
                error: Some(json!({"code":"database_read_failed","message":message})),
            },
        }
    }

    fn execute_read_only(&mut self, request: DatabaseRequest) -> Result<Value, String> {
        let mut transaction = self
            .client
            .build_transaction()
            .read_only(true)
            .start()
            .map_err(|_| "cannot start read-only database transaction".to_string())?;
        let result = match request {
            DatabaseRequest::ServerInfo => {
                let row = transaction.query_one(
                    "SELECT current_database(), current_user, current_setting('server_version'), pg_is_in_recovery()",
                    &[],
                ).map_err(|_| "cannot inspect database server".to_string())?;
                json!({
                    "database": row.get::<_, String>(0),
                    "role": row.get::<_, String>(1),
                    "server_version": row.get::<_, String>(2),
                    "in_recovery": row.get::<_, bool>(3),
                    "transaction_read_only": true,
                })
            }
            DatabaseRequest::ListSchemas => {
                let rows = transaction.query(
                    "SELECT nspname FROM pg_catalog.pg_namespace WHERE nspname !~ '^pg_' AND nspname <> 'information_schema' ORDER BY nspname LIMIT 500",
                    &[],
                ).map_err(|_| "cannot list database schemas".to_string())?;
                json!({"schemas": rows.into_iter().map(|row| row.get::<_,String>(0)).collect::<Vec<_>>()})
            }
            DatabaseRequest::SchemaSnapshot { schema } => {
                validate_identifier(&schema)?;
                let rows = transaction.query(
                    "SELECT table_name,column_name,data_type,is_nullable,ordinal_position FROM information_schema.columns WHERE table_schema=$1 ORDER BY table_name,ordinal_position LIMIT $2",
                    &[&schema, &(MAX_SCHEMA_ROWS as i64)],
                ).map_err(|_| "cannot inspect database schema".to_string())?;
                let columns = rows.into_iter().map(|row| json!({
                    "table":row.get::<_,String>(0),"column":row.get::<_,String>(1),
                    "data_type":row.get::<_,String>(2),"nullable":row.get::<_,String>(3)=="YES",
                    "position":row.get::<_,i32>(4),
                })).collect::<Vec<_>>();
                json!({"schema":schema,"columns":columns,"truncated":columns.len()>=MAX_SCHEMA_ROWS})
            }
            DatabaseRequest::TableSchema { schema, table } => {
                validate_identifier(&schema)?;
                validate_identifier(&table)?;
                let rows = transaction.query(
                    "SELECT column_name,data_type,is_nullable,column_default,ordinal_position FROM information_schema.columns WHERE table_schema=$1 AND table_name=$2 ORDER BY ordinal_position LIMIT 1000",
                    &[&schema,&table],
                ).map_err(|_| "cannot inspect database table".to_string())?;
                let columns = rows
                    .into_iter()
                    .map(|row| {
                        json!({
                            "column":row.get::<_,String>(0),"data_type":row.get::<_,String>(1),
                            "nullable":row.get::<_,String>(2)=="YES",
                            "default":row.get::<_,Option<String>>(3),"position":row.get::<_,i32>(4),
                        })
                    })
                    .collect::<Vec<_>>();
                json!({"schema":schema,"table":table,"columns":columns})
            }
            DatabaseRequest::ExtensionInventory => {
                let rows = transaction.query(
                    "SELECT extname,extversion FROM pg_catalog.pg_extension ORDER BY extname LIMIT 500",
                    &[],
                ).map_err(|_| "cannot inspect database extensions".to_string())?;
                json!({"extensions":rows.into_iter().map(|row|json!({
                    "name":row.get::<_,String>(0),"version":row.get::<_,String>(1)
                })).collect::<Vec<_>>()})
            }
        };
        transaction
            .commit()
            .map_err(|_| "cannot close read-only transaction".to_string())?;
        Ok(result)
    }
}

fn validate_identifier(value: &str) -> Result<(), String> {
    if value.is_empty()
        || value.len() > 63
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'_')
    {
        return Err("database identifier is invalid".to_string());
    }
    Ok(())
}

fn default_schema() -> String {
    "public".to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn identifiers_cannot_inject_sql() {
        assert!(validate_identifier("reporting").is_ok());
        assert!(validate_identifier("public; DROP TABLE users").is_err());
    }
}
