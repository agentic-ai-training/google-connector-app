"""Initial complete schema.

Revision ID: 001
"""
from alembic import op

revision = "001"
down_revision = None
branch_labels = None
depends_on = None

DDL = r'''
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE TABLE gmail_messages (id TEXT PRIMARY KEY, thread_id TEXT, sender TEXT, sender_name TEXT, recipients TEXT[], subject TEXT, body_plain TEXT, body_html TEXT, labels TEXT[], has_attachments BOOLEAN DEFAULT FALSE, attachment_names TEXT[], received_at TIMESTAMPTZ, is_read BOOLEAN DEFAULT FALSE, is_starred BOOLEAN DEFAULT FALSE, snippet TEXT, embedding vector(768), synced_at TIMESTAMPTZ DEFAULT now());
CREATE INDEX gmail_embedding_idx ON gmail_messages USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=64);
CREATE INDEX gmail_received_idx ON gmail_messages (received_at DESC); CREATE INDEX gmail_sender_idx ON gmail_messages(sender); CREATE INDEX gmail_labels_idx ON gmail_messages USING GIN(labels);
CREATE TABLE calendar_events (id TEXT PRIMARY KEY, calendar_id TEXT NOT NULL DEFAULT 'primary', title TEXT, description TEXT, location TEXT, start_time TIMESTAMPTZ, end_time TIMESTAMPTZ, is_all_day BOOLEAN DEFAULT FALSE, attendees JSONB, organizer_email TEXT, meet_link TEXT, status TEXT, recurrence TEXT[], embedding vector(768), synced_at TIMESTAMPTZ DEFAULT now());
CREATE INDEX calendar_start_idx ON calendar_events(start_time); CREATE INDEX calendar_end_idx ON calendar_events(end_time);
CREATE TABLE drive_documents (id TEXT PRIMARY KEY, name TEXT, mime_type TEXT, content TEXT, parent_folder TEXT, web_view_link TEXT, owners TEXT[], shared_with TEXT[], size_bytes BIGINT, modified_at TIMESTAMPTZ, created_at TIMESTAMPTZ, trashed BOOLEAN DEFAULT FALSE, embedding vector(768), synced_at TIMESTAMPTZ DEFAULT now());
CREATE INDEX drive_embedding_idx ON drive_documents USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=64); CREATE INDEX drive_modified_idx ON drive_documents(modified_at DESC); CREATE INDEX drive_mime_idx ON drive_documents(mime_type);
CREATE TABLE contacts (id TEXT PRIMARY KEY, display_name TEXT, emails TEXT[], phone_numbers TEXT[], organization TEXT, job_title TEXT, notes TEXT, photo_url TEXT, embedding vector(768), synced_at TIMESTAMPTZ DEFAULT now());
CREATE INDEX contacts_name_idx ON contacts(display_name); CREATE INDEX contacts_emails_idx ON contacts USING GIN(emails);
CREATE TABLE chat_messages (id TEXT PRIMARY KEY, space_id TEXT, space_name TEXT, sender_email TEXT, sender_name TEXT, text TEXT, thread_id TEXT, created_at TIMESTAMPTZ, embedding vector(768), synced_at TIMESTAMPTZ DEFAULT now());
CREATE INDEX chat_space_idx ON chat_messages(space_id); CREATE INDEX chat_created_idx ON chat_messages(created_at DESC);
CREATE TABLE tasks (id TEXT PRIMARY KEY, tasklist_id TEXT, tasklist_name TEXT, title TEXT, notes TEXT, status TEXT, due_date TIMESTAMPTZ, completed_at TIMESTAMPTZ, parent_task_id TEXT, position TEXT, synced_at TIMESTAMPTZ DEFAULT now());
CREATE INDEX tasks_status_idx ON tasks(status); CREATE INDEX tasks_due_idx ON tasks(due_date);
CREATE TABLE conversation_history (id UUID PRIMARY KEY DEFAULT uuid_generate_v4(), session_id TEXT NOT NULL, user_id TEXT, role TEXT NOT NULL, content TEXT NOT NULL, tool_calls JSONB, tool_results JSONB, model_used TEXT, tokens_used INTEGER, created_at TIMESTAMPTZ DEFAULT now());
CREATE INDEX conv_session_idx ON conversation_history(session_id); CREATE INDEX conv_created_idx ON conversation_history(created_at DESC);
CREATE TABLE user_preferences (user_id TEXT PRIMARY KEY, email TEXT, timezone TEXT DEFAULT 'Asia/Kolkata', preferences JSONB DEFAULT '{}', created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ DEFAULT now());
CREATE TABLE prompts (id UUID PRIMARY KEY DEFAULT uuid_generate_v4(), name TEXT NOT NULL, version INTEGER NOT NULL, content TEXT NOT NULL, model_target TEXT NOT NULL, temperature FLOAT DEFAULT .3, max_tokens INTEGER DEFAULT 1000, is_active BOOLEAN DEFAULT FALSE, created_at TIMESTAMPTZ DEFAULT now(), created_by TEXT DEFAULT 'system', notes TEXT, UNIQUE(name,version));
CREATE UNIQUE INDEX one_active_prompt ON prompts(name,model_target) WHERE is_active=TRUE;
CREATE TABLE prompt_experiments (id UUID PRIMARY KEY DEFAULT uuid_generate_v4(), name TEXT NOT NULL UNIQUE, prompt_name TEXT NOT NULL, control_id UUID REFERENCES prompts(id), variant_id UUID REFERENCES prompts(id), traffic_split FLOAT DEFAULT .5, status TEXT DEFAULT 'running', winner TEXT, started_at TIMESTAMPTZ DEFAULT now(), ended_at TIMESTAMPTZ, notes TEXT);
CREATE TABLE prompt_assignments (id UUID PRIMARY KEY DEFAULT uuid_generate_v4(), session_id TEXT NOT NULL, experiment_id UUID REFERENCES prompt_experiments(id), prompt_id UUID REFERENCES prompts(id), arm TEXT NOT NULL, assigned_at TIMESTAMPTZ DEFAULT now());
CREATE UNIQUE INDEX sticky_assignment ON prompt_assignments(session_id,experiment_id);
CREATE TABLE task_log (id UUID PRIMARY KEY DEFAULT uuid_generate_v4(), session_id TEXT, user_id TEXT, task_type TEXT, tool_name TEXT, input_data JSONB, output_data JSONB, status TEXT DEFAULT 'pending', error_message TEXT, llm_latency_ms INTEGER, total_latency_ms INTEGER, input_tokens INTEGER, output_tokens INTEGER, model_used TEXT, executed_at TIMESTAMPTZ DEFAULT now());
CREATE INDEX tasklog_session_idx ON task_log(session_id); CREATE INDEX tasklog_status_idx ON task_log(status); CREATE INDEX tasklog_executed_idx ON task_log(executed_at DESC);
CREATE TABLE feedback (id UUID PRIMARY KEY DEFAULT uuid_generate_v4(), session_id TEXT, user_id TEXT, user_question TEXT, agent_response TEXT, retrieved_docs JSONB, rating INTEGER, comment TEXT, prompt_id UUID REFERENCES prompts(id), assignment_id UUID REFERENCES prompt_assignments(id), created_at TIMESTAMPTZ DEFAULT now());
CREATE TABLE prompt_metrics (id UUID PRIMARY KEY DEFAULT uuid_generate_v4(), assignment_id UUID REFERENCES prompt_assignments(id), prompt_id UUID REFERENCES prompts(id), session_id TEXT, llm_latency_ms INTEGER, total_latency_ms INTEGER, input_tokens INTEGER, output_tokens INTEGER, faithfulness FLOAT, answer_relevancy FLOAT, context_recall FLOAT, user_rating INTEGER, task_completed BOOLEAN, error_occurred BOOLEAN DEFAULT FALSE, error_type TEXT, recorded_at TIMESTAMPTZ DEFAULT now());
CREATE TABLE sync_log (id UUID PRIMARY KEY DEFAULT uuid_generate_v4(), source TEXT NOT NULL, last_synced_at TIMESTAMPTZ, items_synced INTEGER DEFAULT 0, items_embedded INTEGER DEFAULT 0, status TEXT DEFAULT 'success', error_message TEXT, duration_ms INTEGER, ran_at TIMESTAMPTZ DEFAULT now());
CREATE VIEW experiment_summary AS SELECT e.name experiment_name,e.status,pa.arm,COUNT(*) total_requests,ROUND(AVG(pm.llm_latency_ms)::numeric,0) avg_latency_ms,ROUND(AVG(pm.user_rating)::numeric,3) avg_rating,ROUND(AVG(pm.faithfulness)::numeric,3) avg_faithfulness,ROUND(AVG(pm.answer_relevancy)::numeric,3) avg_relevancy,ROUND(100.0*SUM(CASE WHEN pm.error_occurred THEN 1 ELSE 0 END)/NULLIF(COUNT(*),0),2) error_rate_pct,ROUND(100.0*SUM(CASE WHEN pm.task_completed THEN 1 ELSE 0 END)/NULLIF(COUNT(*),0),2) completion_rate_pct FROM prompt_experiments e JOIN prompt_assignments pa ON pa.experiment_id=e.id JOIN prompt_metrics pm ON pm.assignment_id=pa.id GROUP BY e.name,e.status,pa.arm;
INSERT INTO prompts(name,version,content,model_target,is_active,notes) VALUES
('supervisor_system',1,'You are an intelligent Google Workspace assistant with access to Gmail, Calendar, Drive, Docs, Sheets, Tasks, Chat, Contacts, and Apps Script. When the user gives a command: (1) identify which Google services are needed, (2) break the task into sequential tool calls, (3) execute tools one at a time, (4) verify each result before proceeding, (5) confirm completion concisely. For complex reasoning tasks, think step by step. Always be action-oriented and brief.','groq/llama-3.3-70b',TRUE,'Initial production supervisor prompt v1'),
('supervisor_system',2,'You are a precise Google Workspace automation agent. Analyse the user command carefully before acting. Identify all required services and plan the full sequence of tool calls. Execute one tool at a time. After each tool call, verify the result. If a step fails, explain why and propose an alternative. For tasks requiring deep reasoning (writing, analysis, planning), engage DeepSeek R1. Keep all responses brief and action-oriented.','groq/llama-3.3-70b',FALSE,'V2 — adds explicit planning step and DeepSeek routing hint');
'''

def upgrade():
    op.execute(DDL)

def downgrade():
    op.execute("DROP VIEW IF EXISTS experiment_summary CASCADE; " + ";".join(f"DROP TABLE IF EXISTS {t} CASCADE" for t in ['sync_log','prompt_metrics','feedback','task_log','prompt_assignments','prompt_experiments','prompts','user_preferences','conversation_history','tasks','chat_messages','contacts','drive_documents','calendar_events','gmail_messages']))
