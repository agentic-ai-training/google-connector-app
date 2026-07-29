# Google Workspace AI Agent — Complete Project Specification
> For Codex: Execute every epic, story, and task in order. Do not skip steps. Do not assume anything is already done unless explicitly marked DONE. After every sprint, run verification commands and confirm output before proceeding.

---

## Project Context

**What is being built:** A production-grade AI agent that connects to all major Google Workspace APIs, uses LangGraph for agent orchestration, LangChain for tool definitions, Groq (LLaMA 3.3 70B) and DeepSeek R1 as LLM backends, pgvector on local Postgres for RAG, and exposes a FastAPI backend consumed by a Flutter mobile app and Next.js web app.

**Current state (already done by human):**
- `google-connector-app/` project folder exists on Mac
- `.venv/` Python virtual environment created with Python 3
- `credentials.json` present in project root (Google OAuth Desktop App credentials)
- `token.pkl` present (OAuth already completed for achintyat256@gmail.com)
- `test_phase1.py` present and verified — all 6 Google APIs return data
- `.gitignore` created with: credentials.json, token.pkl, .venv/, __pycache__/, .env, *.pyc
- Local Postgres installed (v17 or v18 via Homebrew)
- Git NOT yet initialised
- GitHub repo NOT yet created

**Developer machine:** MacBook Air, zsh shell, Homebrew installed, Python 3 available as `python3`

**LLM API keys needed in .env:**
- GROQ_API_KEY — from console.groq.com
- DEEPSEEK_API_KEY — from platform.deepseek.com

---

## Technology Stack

| Layer | Technology |
|---|---|
| Agent orchestration | LangGraph |
| Tool framework | LangChain |
| Primary LLM (fast) | Groq — LLaMA 3.3 70B Versatile |
| Secondary LLM (reasoning) | DeepSeek R1 (free tier) |
| Embeddings | nomic-embed-text via Ollama (local, free) |
| Vector + SQL DB | Local Postgres + pgvector extension |
| DB migrations | Alembic |
| Backend API | FastAPI + uvicorn |
| Async DB driver | asyncpg |
| Mobile app | Flutter (Dart) — Android + iOS |
| Web app | Next.js 14 (React) |
| Observability | LangSmith (free tier) |
| RAG evaluation | RAGAS |
| Metrics | Prometheus + Grafana |
| Scheduled jobs | APScheduler |
| CI/CD | GitHub Actions |
| Production backend | Railway (free tier) |
| Production DB | Neon.tech (free tier, pgvector built-in) |
| Production web | Vercel (free tier) |

---

## Full Phase List

- **Phase 0** — DONE (architecture and schema design, completed in planning)
- **Phase 1** — DONE (Google OAuth and API verification)
- **Phase 2** — Git + GitHub setup
- **Phase 3** — Local Postgres + pgvector + full schema + Alembic migrations
- **Phase 4** — Project structure scaffold + dependencies
- **Phase 5** — Google API LangChain tool wrappers (all 12 services)
- **Phase 6** — FastAPI backend (REST + SSE + JWT + LangSmith)
- **Phase 7** — LangGraph agent graph (supervisor + subagents + routing)
- **Phase 8** — RAG pipeline (embedding + pgvector + hybrid retrieval + sync job)
- **Phase 9** — Prompt versioning + A/B testing system
- **Phase 10** — MLOps layer (LangSmith + RAGAS + Prometheus + feedback flywheel)
- **Phase 11** — Next.js web app
- **Phase 12** — Flutter mobile app
- **Phase 13** — Production deployment (Railway + Neon + Vercel + GitHub Actions)

---

## SPRINT 1 — Git and GitHub Setup

### Epic 1.1 — Initialise local git repository

**Story 1.1.1 — Init git in project root**
```bash
cd /Users/achintyatyagi/google-connector-app
git init
git add .
git commit -m "Phase 1 complete — Google APIs verified, project scaffold"
```
Verify: `git log --oneline` shows 1 commit.

**Story 1.1.2 — Create .env template file (no real keys yet)**
Create file `.env.example` in project root:
```
GROQ_API_KEY=your_groq_key_here
DEEPSEEK_API_KEY=your_deepseek_key_here
DATABASE_URL=postgresql://agent_user:yourpassword@localhost:5432/agent_db
LANGCHAIN_TRACING_V2=true
LANGCHAIN_API_KEY=your_langsmith_key_here
LANGCHAIN_PROJECT=google-agent
GOOGLE_CREDENTIALS_PATH=./credentials.json
```
Create actual `.env` file with the same structure but with real keys filled in by developer.
Verify `.env` is in `.gitignore` and does NOT appear in `git status`.

### Epic 1.2 — Create GitHub repository and push

**Story 1.2.1 — Install GitHub CLI if not present**
```bash
brew install gh
gh --version
```

**Story 1.2.2 — Authenticate GitHub CLI**
```bash
gh auth login
```
Select: GitHub.com → HTTPS → Login with a web browser → follow prompts.
Verify: `gh auth status` shows logged in.

**Story 1.2.3 — Create remote repo and push**
```bash
cd /Users/achintyatyagi/google-connector-app
gh repo create google-connector-app \
  --private \
  --source=. \
  --remote=origin \
  --push \
  --description "Production-grade Google Workspace AI Agent with LangGraph, RAG, and Flutter"
```
Verify: `git remote -v` shows origin pointing to github.com. `gh repo view` opens the repo page.

**Story 1.2.4 — Protect main branch**
```bash
gh api repos/{owner}/google-connector-app/branches/main/protection \
  --method PUT \
  --field required_status_checks=null \
  --field enforce_admins=false \
  --field required_pull_request_reviews=null \
  --field restrictions=null
```

---

## SPRINT 2 — Local Postgres + pgvector + Full Schema

### Epic 2.1 — Postgres setup

**Story 2.1.1 — Start Postgres service**
```bash
brew services start postgresql@17
# or if v18:
brew services start postgresql@18
brew services list | grep postgresql
```
Verify: status shows `started`.

**Story 2.1.2 — Create database and user**
```bash
psql postgres -c "CREATE DATABASE agent_db;"
psql postgres -c "CREATE USER agent_user WITH PASSWORD 'agent_pass_2024';"
psql postgres -c "GRANT ALL PRIVILEGES ON DATABASE agent_db TO agent_user;"
psql postgres -c "ALTER DATABASE agent_db OWNER TO agent_user;"
```

**Story 2.1.3 — Install pgvector extension**
```bash
brew install pgvector
psql -U agent_user -d agent_db -c "CREATE EXTENSION IF NOT EXISTS vector;"
psql -U agent_user -d agent_db -c "CREATE EXTENSION IF NOT EXISTS \"uuid-ossp\";"
```
Verify both return `CREATE EXTENSION`.

**Story 2.1.4 — Update .env with database URL**
Add to `.env`:
```
DATABASE_URL=postgresql://agent_user:agent_pass_2024@localhost:5432/agent_db
ASYNC_DATABASE_URL=postgresql+asyncpg://agent_user:agent_pass_2024@localhost:5432/agent_db
```

### Epic 2.2 — Full database schema

**Story 2.2.1 — Install Python dependencies for DB**
```bash
cd /Users/achintyatyagi/google-connector-app
source .venv/bin/activate
pip install asyncpg psycopg2-binary pgvector alembic sqlalchemy python-dotenv
```

**Story 2.2.2 — Initialise Alembic**
```bash
alembic init migrations
```
Edit `alembic.ini` — set `sqlalchemy.url` to:
```
postgresql://agent_user:agent_pass_2024@localhost:5432/agent_db
```
Edit `migrations/env.py` to import and use your models' metadata.

**Story 2.2.3 — Create full schema migration**

Create file `migrations/versions/001_initial_schema.py` with the following complete DDL:

```sql
-- EXTENSION (already created, idempotent)
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ── GMAIL ───────────────────────────────────────────────────
CREATE TABLE gmail_messages (
    id              TEXT PRIMARY KEY,
    thread_id       TEXT,
    sender          TEXT,
    sender_name     TEXT,
    recipients      TEXT[],
    subject         TEXT,
    body_plain      TEXT,
    body_html       TEXT,
    labels          TEXT[],
    has_attachments BOOLEAN DEFAULT FALSE,
    attachment_names TEXT[],
    received_at     TIMESTAMPTZ,
    is_read         BOOLEAN DEFAULT FALSE,
    is_starred      BOOLEAN DEFAULT FALSE,
    snippet         TEXT,
    embedding       vector(768),
    synced_at       TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX gmail_embedding_idx ON gmail_messages
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
CREATE INDEX gmail_received_idx ON gmail_messages (received_at DESC);
CREATE INDEX gmail_sender_idx ON gmail_messages (sender);
CREATE INDEX gmail_labels_idx ON gmail_messages USING GIN (labels);

-- ── CALENDAR ────────────────────────────────────────────────
CREATE TABLE calendar_events (
    id              TEXT PRIMARY KEY,
    calendar_id     TEXT NOT NULL DEFAULT 'primary',
    title           TEXT,
    description     TEXT,
    location        TEXT,
    start_time      TIMESTAMPTZ,
    end_time        TIMESTAMPTZ,
    is_all_day      BOOLEAN DEFAULT FALSE,
    attendees       JSONB,
    organizer_email TEXT,
    meet_link       TEXT,
    status          TEXT,
    recurrence      TEXT[],
    embedding       vector(768),
    synced_at       TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX calendar_start_idx ON calendar_events (start_time);
CREATE INDEX calendar_end_idx ON calendar_events (end_time);

-- ── DRIVE ───────────────────────────────────────────────────
CREATE TABLE drive_documents (
    id              TEXT PRIMARY KEY,
    name            TEXT,
    mime_type       TEXT,
    content         TEXT,
    parent_folder   TEXT,
    web_view_link   TEXT,
    owners          TEXT[],
    shared_with     TEXT[],
    size_bytes      BIGINT,
    modified_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ,
    trashed         BOOLEAN DEFAULT FALSE,
    embedding       vector(768),
    synced_at       TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX drive_embedding_idx ON drive_documents
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
CREATE INDEX drive_modified_idx ON drive_documents (modified_at DESC);
CREATE INDEX drive_mime_idx ON drive_documents (mime_type);

-- ── CONTACTS ────────────────────────────────────────────────
CREATE TABLE contacts (
    id              TEXT PRIMARY KEY,
    display_name    TEXT,
    emails          TEXT[],
    phone_numbers   TEXT[],
    organization    TEXT,
    job_title       TEXT,
    notes           TEXT,
    photo_url       TEXT,
    embedding       vector(768),
    synced_at       TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX contacts_name_idx ON contacts (display_name);
CREATE INDEX contacts_emails_idx ON contacts USING GIN (emails);

-- ── CHAT ────────────────────────────────────────────────────
CREATE TABLE chat_messages (
    id              TEXT PRIMARY KEY,
    space_id        TEXT,
    space_name      TEXT,
    sender_email    TEXT,
    sender_name     TEXT,
    text            TEXT,
    thread_id       TEXT,
    created_at      TIMESTAMPTZ,
    embedding       vector(768),
    synced_at       TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX chat_space_idx ON chat_messages (space_id);
CREATE INDEX chat_created_idx ON chat_messages (created_at DESC);

-- ── TASKS ───────────────────────────────────────────────────
CREATE TABLE tasks (
    id              TEXT PRIMARY KEY,
    tasklist_id     TEXT,
    tasklist_name   TEXT,
    title           TEXT,
    notes           TEXT,
    status          TEXT,
    due_date        TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    parent_task_id  TEXT,
    position        TEXT,
    synced_at       TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX tasks_status_idx ON tasks (status);
CREATE INDEX tasks_due_idx ON tasks (due_date);

-- ── CONVERSATION HISTORY ────────────────────────────────────
CREATE TABLE conversation_history (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    session_id      TEXT NOT NULL,
    user_id         TEXT,
    role            TEXT NOT NULL,
    content         TEXT NOT NULL,
    tool_calls      JSONB,
    tool_results    JSONB,
    model_used      TEXT,
    tokens_used     INTEGER,
    created_at      TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX conv_session_idx ON conversation_history (session_id);
CREATE INDEX conv_created_idx ON conversation_history (created_at DESC);

-- ── USER PREFERENCES ────────────────────────────────────────
CREATE TABLE user_preferences (
    user_id         TEXT PRIMARY KEY,
    email           TEXT,
    timezone        TEXT DEFAULT 'Asia/Kolkata',
    preferences     JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

-- ── PROMPTS (versioning + A/B) ──────────────────────────────
CREATE TABLE prompts (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name            TEXT NOT NULL,
    version         INTEGER NOT NULL,
    content         TEXT NOT NULL,
    model_target    TEXT NOT NULL,
    temperature     FLOAT DEFAULT 0.3,
    max_tokens      INTEGER DEFAULT 1000,
    is_active       BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMPTZ DEFAULT now(),
    created_by      TEXT DEFAULT 'system',
    notes           TEXT,
    UNIQUE (name, version)
);
CREATE UNIQUE INDEX one_active_prompt ON prompts (name, model_target)
    WHERE is_active = TRUE;

CREATE TABLE prompt_experiments (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name            TEXT NOT NULL UNIQUE,
    prompt_name     TEXT NOT NULL,
    control_id      UUID REFERENCES prompts(id),
    variant_id      UUID REFERENCES prompts(id),
    traffic_split   FLOAT DEFAULT 0.5,
    status          TEXT DEFAULT 'running',
    winner          TEXT,
    started_at      TIMESTAMPTZ DEFAULT now(),
    ended_at        TIMESTAMPTZ,
    notes           TEXT
);

CREATE TABLE prompt_assignments (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    session_id      TEXT NOT NULL,
    experiment_id   UUID REFERENCES prompt_experiments(id),
    prompt_id       UUID REFERENCES prompts(id),
    arm             TEXT NOT NULL,
    assigned_at     TIMESTAMPTZ DEFAULT now()
);
CREATE UNIQUE INDEX sticky_assignment ON prompt_assignments (session_id, experiment_id);

-- ── TASK LOG ────────────────────────────────────────────────
CREATE TABLE task_log (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    session_id      TEXT,
    user_id         TEXT,
    task_type       TEXT,
    tool_name       TEXT,
    input_data      JSONB,
    output_data     JSONB,
    status          TEXT DEFAULT 'pending',
    error_message   TEXT,
    llm_latency_ms  INTEGER,
    total_latency_ms INTEGER,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    model_used      TEXT,
    executed_at     TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX tasklog_session_idx ON task_log (session_id);
CREATE INDEX tasklog_status_idx ON task_log (status);
CREATE INDEX tasklog_executed_idx ON task_log (executed_at DESC);

-- ── FEEDBACK (DPO flywheel) ─────────────────────────────────
CREATE TABLE feedback (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    session_id      TEXT,
    user_id         TEXT,
    user_question   TEXT,
    agent_response  TEXT,
    retrieved_docs  JSONB,
    rating          INTEGER,
    comment         TEXT,
    prompt_id       UUID REFERENCES prompts(id),
    assignment_id   UUID REFERENCES prompt_assignments(id),
    created_at      TIMESTAMPTZ DEFAULT now()
);

-- ── PROMPT METRICS ──────────────────────────────────────────
CREATE TABLE prompt_metrics (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    assignment_id   UUID REFERENCES prompt_assignments(id),
    prompt_id       UUID REFERENCES prompts(id),
    session_id      TEXT,
    llm_latency_ms  INTEGER,
    total_latency_ms INTEGER,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    faithfulness    FLOAT,
    answer_relevancy FLOAT,
    context_recall  FLOAT,
    user_rating     INTEGER,
    task_completed  BOOLEAN,
    error_occurred  BOOLEAN DEFAULT FALSE,
    error_type      TEXT,
    recorded_at     TIMESTAMPTZ DEFAULT now()
);

-- ── SYNC LOG ────────────────────────────────────────────────
CREATE TABLE sync_log (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    source          TEXT NOT NULL,
    last_synced_at  TIMESTAMPTZ,
    items_synced    INTEGER DEFAULT 0,
    items_embedded  INTEGER DEFAULT 0,
    status          TEXT DEFAULT 'success',
    error_message   TEXT,
    duration_ms     INTEGER,
    ran_at          TIMESTAMPTZ DEFAULT now()
);

-- ── EXPERIMENT SUMMARY VIEW ─────────────────────────────────
CREATE VIEW experiment_summary AS
SELECT
    e.name                                              AS experiment_name,
    e.status,
    pa.arm,
    COUNT(*)                                            AS total_requests,
    ROUND(AVG(pm.llm_latency_ms)::numeric, 0)          AS avg_latency_ms,
    ROUND(AVG(pm.user_rating)::numeric, 3)              AS avg_rating,
    ROUND(AVG(pm.faithfulness)::numeric, 3)             AS avg_faithfulness,
    ROUND(AVG(pm.answer_relevancy)::numeric, 3)         AS avg_relevancy,
    ROUND(100.0 * SUM(CASE WHEN pm.error_occurred THEN 1 ELSE 0 END)
        / NULLIF(COUNT(*), 0), 2)                       AS error_rate_pct,
    ROUND(100.0 * SUM(CASE WHEN pm.task_completed THEN 1 ELSE 0 END)
        / NULLIF(COUNT(*), 0), 2)                       AS completion_rate_pct
FROM prompt_experiments e
JOIN prompt_assignments pa ON pa.experiment_id = e.id
JOIN prompt_metrics pm ON pm.assignment_id = pa.id
GROUP BY e.name, e.status, pa.arm;

-- ── SEED INITIAL PROMPTS ────────────────────────────────────
INSERT INTO prompts (name, version, content, model_target, is_active, notes)
VALUES (
    'supervisor_system', 1,
    'You are an intelligent Google Workspace assistant with access to Gmail, Calendar, Drive, Docs, Sheets, Tasks, Chat, Contacts, and Apps Script. When the user gives a command: (1) identify which Google services are needed, (2) break the task into sequential tool calls, (3) execute tools one at a time, (4) verify each result before proceeding, (5) confirm completion concisely. For complex reasoning tasks, think step by step. Always be action-oriented and brief.',
    'groq/llama-3.3-70b',
    TRUE,
    'Initial production supervisor prompt v1'
),
(
    'supervisor_system', 2,
    'You are a precise Google Workspace automation agent. Analyse the user command carefully before acting. Identify all required services and plan the full sequence of tool calls. Execute one tool at a time. After each tool call, verify the result. If a step fails, explain why and propose an alternative. For tasks requiring deep reasoning (writing, analysis, planning), engage DeepSeek R1. Keep all responses brief and action-oriented.',
    'groq/llama-3.3-70b',
    FALSE,
    'V2 — adds explicit planning step and DeepSeek routing hint'
);
```

Run the migration:
```bash
alembic revision --autogenerate -m "initial_schema"
alembic upgrade head
```

**Story 2.2.4 — Verify schema**
```bash
psql -U agent_user -d agent_db -c "\dt"
psql -U agent_user -d agent_db -c "\di"
```
Verify all 14 tables and all indexes appear.

---

## SPRINT 3 — Project Structure and Core Dependencies

### Epic 3.1 — Full project folder structure

**Story 3.1.1 — Create complete directory tree**
```bash
cd /Users/achintyatyagi/google-connector-app
mkdir -p app/{api,agents,tools/{gmail,calendar,drive,docs,sheets,tasks,chat,contacts,scripts},rag,db,mlops,config}
mkdir -p tests/{unit,integration}
mkdir -p scripts
mkdir -p .github/workflows
```

Final structure must be:
```
google-connector-app/
├── app/
│   ├── api/
│   │   ├── __init__.py
│   │   ├── main.py              # FastAPI app entry point
│   │   ├── routes/
│   │   │   ├── chat.py          # POST /chat SSE endpoint
│   │   │   ├── feedback.py      # POST /feedback
│   │   │   ├── history.py       # GET /history
│   │   │   └── admin.py         # prompt experiment management
│   │   └── middleware/
│   │       ├── auth.py          # JWT middleware
│   │       └── metrics.py       # Prometheus middleware
│   ├── agents/
│   │   ├── __init__.py
│   │   ├── supervisor.py        # LangGraph supervisor node
│   │   ├── router.py            # model routing (Groq vs DeepSeek)
│   │   ├── subagents/
│   │   │   ├── gmail_agent.py
│   │   │   ├── calendar_agent.py
│   │   │   ├── drive_agent.py
│   │   │   ├── docs_agent.py
│   │   │   ├── sheets_agent.py
│   │   │   ├── tasks_agent.py
│   │   │   ├── chat_agent.py
│   │   │   └── contacts_agent.py
│   │   └── state.py             # LangGraph state definition
│   ├── tools/
│   │   ├── __init__.py
│   │   ├── base.py              # BaseTool with pgvector upsert + task_log
│   │   ├── gmail/
│   │   │   ├── search_emails.py
│   │   │   ├── get_email.py
│   │   │   ├── send_email.py
│   │   │   ├── reply_email.py
│   │   │   ├── label_email.py
│   │   │   ├── trash_email.py
│   │   │   └── list_threads.py
│   │   ├── calendar/
│   │   │   ├── list_events.py
│   │   │   ├── get_event.py
│   │   │   ├── create_event.py
│   │   │   ├── update_event.py
│   │   │   ├── delete_event.py
│   │   │   └── check_availability.py
│   │   ├── drive/
│   │   │   ├── search_files.py
│   │   │   ├── get_file.py
│   │   │   ├── upload_file.py
│   │   │   ├── share_file.py
│   │   │   └── move_file.py
│   │   ├── docs/
│   │   │   ├── read_doc.py
│   │   │   ├── create_doc.py
│   │   │   └── append_to_doc.py
│   │   ├── sheets/
│   │   │   ├── read_sheet.py
│   │   │   ├── write_sheet.py
│   │   │   ├── append_rows.py
│   │   │   └── create_sheet.py
│   │   ├── tasks/
│   │   │   ├── list_tasks.py
│   │   │   ├── create_task.py
│   │   │   └── complete_task.py
│   │   ├── chat/
│   │   │   ├── list_spaces.py
│   │   │   └── send_message.py
│   │   └── contacts/
│   │       ├── search_contacts.py
│   │       └── get_contact.py
│   ├── rag/
│   │   ├── __init__.py
│   │   ├── embedder.py          # nomic-embed-text via Ollama
│   │   ├── retriever.py         # hybrid pgvector + SQL retrieval
│   │   ├── context_packer.py    # pack retrieved docs into LLM context window
│   │   └── sync/
│   │       ├── gmail_sync.py
│   │       ├── drive_sync.py
│   │       ├── calendar_sync.py
│   │       ├── contacts_sync.py
│   │       └── scheduler.py     # APScheduler nightly job
│   ├── db/
│   │   ├── __init__.py
│   │   ├── connection.py        # asyncpg pool
│   │   ├── google_clients.py    # all 12 Google API service clients
│   │   └── prompt_service.py    # prompt versioning + A/B testing
│   ├── mlops/
│   │   ├── __init__.py
│   │   ├── langsmith_config.py
│   │   ├── ragas_eval.py
│   │   └── metrics.py           # Prometheus counters and histograms
│   └── config/
│       ├── __init__.py
│       └── settings.py          # pydantic-settings config from .env
├── tests/
│   ├── unit/
│   └── integration/
├── scripts/
│   └── run_ragas_eval.py
├── migrations/
├── .github/
│   └── workflows/
│       ├── ci.yml               # test on push
│       └── deploy.yml           # deploy on merge to main
├── credentials.json             # gitignored
├── token.pkl                    # gitignored
├── .env                         # gitignored
├── .env.example
├── .gitignore
├── alembic.ini
├── requirements.txt
├── Dockerfile
└── docker-compose.yml
```

**Story 3.1.2 — Create __init__.py in all packages**
```bash
find app -type d -exec touch {}/__init__.py \;
touch tests/__init__.py tests/unit/__init__.py tests/integration/__init__.py
```

### Epic 3.2 — Install all Python dependencies

**Story 3.2.1 — Install complete dependency set**
```bash
source .venv/bin/activate
pip install \
  fastapi uvicorn[standard] \
  asyncpg psycopg2-binary pgvector sqlalchemy alembic \
  langchain langgraph langchain-groq langchain-community langchain-core \
  langchain-postgres \
  google-api-python-client google-auth-httplib2 google-auth-oauthlib \
  python-dotenv pydantic pydantic-settings \
  python-jose[cryptography] passlib[bcrypt] \
  prometheus-client \
  apscheduler \
  ollama \
  ragas datasets \
  httpx pytest pytest-asyncio \
  python-multipart
```

**Story 3.2.2 — Freeze requirements**
```bash
pip freeze > requirements.txt
```

**Story 3.2.3 — Create settings.py**
File: `app/config/settings.py`
```python
from pydantic_settings import BaseSettings
from functools import lru_cache

class Settings(BaseSettings):
    groq_api_key: str
    deepseek_api_key: str
    database_url: str
    async_database_url: str
    langchain_tracing_v2: str = "true"
    langchain_api_key: str = ""
    langchain_project: str = "google-agent"
    google_credentials_path: str = "./credentials.json"
    jwt_secret_key: str = "change-this-in-production"
    jwt_algorithm: str = "HS256"

    class Config:
        env_file = ".env"

@lru_cache()
def get_settings() -> Settings:
    return Settings()
```

---

## SPRINT 4 — Google API Clients and Base Tool

### Epic 4.1 — Google service clients

**Story 4.1.1 — Create google_clients.py**
File: `app/db/google_clients.py`

This file must:
- Load credentials from `token.pkl` (already exists from Phase 1)
- Build all 12 service client objects as module-level singletons
- Handle token refresh automatically
- Export: `gmail_service`, `calendar_service`, `drive_service`, `docs_service`, `sheets_service`, `tasks_service`, `chat_service`, `people_service`, `drive_activity_service`, `meet_service`, `script_service`, `drive_labels_service`

```python
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import pickle, os

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/drive.activity.readonly",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/tasks",
    "https://www.googleapis.com/auth/contacts.readonly",
    "https://www.googleapis.com/auth/contacts",
    "https://www.googleapis.com/auth/chat.messages",
    "https://www.googleapis.com/auth/chat.spaces.readonly",
    "https://www.googleapis.com/auth/chat.spaces.create",
    "https://www.googleapis.com/auth/script.projects",
    "https://www.googleapis.com/auth/script.external_request",
    "https://www.googleapis.com/auth/drive.labels.readonly",
    "https://www.googleapis.com/auth/meetings.space.created",
    "https://www.googleapis.com/auth/meetings.space.readonly",
]

def _load_creds():
    creds = None
    if os.path.exists("token.pkl"):
        with open("token.pkl", "rb") as f:
            creds = pickle.load(f)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open("token.pkl", "wb") as f:
            pickle.dump(creds, f)
    return creds

_creds = _load_creds()

gmail_service          = build("gmail", "v1",          credentials=_creds)
calendar_service       = build("calendar", "v3",       credentials=_creds)
drive_service          = build("drive", "v3",          credentials=_creds)
docs_service           = build("docs", "v1",           credentials=_creds)
sheets_service         = build("sheets", "v4",         credentials=_creds)
tasks_service          = build("tasks", "v1",          credentials=_creds)
people_service         = build("people", "v1",         credentials=_creds)
drive_activity_service = build("driveactivity", "v2",  credentials=_creds)
script_service         = build("script", "v1",         credentials=_creds)
chat_service           = build("chat", "v1",           credentials=_creds)
```

### Epic 4.2 — Base tool class

**Story 4.2.1 — Create base.py**
File: `app/tools/base.py`

Every tool inherits from `GoogleWorkspaceBaseTool`. This base class must:
- Accept `db_pool` (asyncpg pool) and `embedder` (from RAG layer) as dependencies
- Provide `_log_task()` method that writes to `task_log` table
- Provide `_embed_and_upsert()` method that calls nomic-embed-text and upserts to the relevant pgvector table
- Provide `_track_metric()` method that increments Prometheus counters
- Wrap every tool call in try/except, log errors to task_log, re-raise

```python
import time
import asyncpg
from langchain.tools import BaseTool
from app.mlops.metrics import tool_errors, tool_latency

class GoogleWorkspaceBaseTool(BaseTool):
    db_pool: asyncpg.Pool = None
    embedder: object = None

    async def _log_task(self, session_id, tool_name, input_data, 
                         output_data, status, error_msg=None,
                         llm_latency_ms=None, total_latency_ms=None,
                         model_used=None):
        if not self.db_pool:
            return
        async with self.db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO task_log (session_id, tool_name, input_data,
                    output_data, status, error_message, llm_latency_ms,
                    total_latency_ms, model_used)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            """, session_id, tool_name,
                str(input_data), str(output_data),
                status, error_msg,
                llm_latency_ms, total_latency_ms, model_used)

    async def _embed_and_upsert(self, table: str, id_val: str,
                                 text: str, extra_fields: dict):
        if not self.embedder or not self.db_pool:
            return
        embedding = await self.embedder.aembed_query(text)
        async with self.db_pool.acquire() as conn:
            fields = ", ".join(extra_fields.keys())
            placeholders = ", ".join(
                f"${i+3}" for i in range(len(extra_fields))
            )
            await conn.execute(f"""
                INSERT INTO {table} (id, embedding, {fields})
                VALUES ($1, $2, {placeholders})
                ON CONFLICT (id) DO UPDATE SET
                    embedding = EXCLUDED.embedding,
                    {", ".join(f"{k} = EXCLUDED.{k}" for k in extra_fields)},
                    synced_at = now()
            """, id_val, embedding, *extra_fields.values())
```

---

## SPRINT 5 — All Google API Tool Wrappers

### Epic 5.1 — Gmail tools

Create one file per tool in `app/tools/gmail/`. Each tool must:
1. Call the Google API using `gmail_service`
2. Parse the response into a clean dict
3. Call `_embed_and_upsert()` with the email content
4. Call `_log_task()` with input/output/status
5. Return a clean string summary for the LLM

**Story 5.1.1 — search_emails.py**
LangChain tool name: `search_gmail`
Input schema: `query: str, max_results: int = 10, after_date: str = None`
Google API call: `gmail_service.users().messages().list(userId='me', q=query, maxResults=max_results)`
For each message ID returned, fetch full message with `gmail_service.users().messages().get(userId='me', id=msg_id, format='full')`
Parse: id, thread_id, sender, subject, snippet, body, labels, received_at
Embed: concatenate subject + body plain text
Upsert to: `gmail_messages`
Return: formatted list of matching emails with id, sender, subject, date, snippet

**Story 5.1.2 — get_email.py**
Tool name: `get_gmail_message`
Input: `message_id: str`
Fetches full email by ID, parses all fields including attachments list
Returns full email content as formatted string

**Story 5.1.3 — send_email.py**
Tool name: `send_gmail`
Input: `to: str, subject: str, body: str, cc: str = None`
Creates MIME message, encodes as base64, calls `gmail_service.users().messages().send()`
Logs to task_log with status
Returns: confirmation with sent message ID

**Story 5.1.4 — reply_email.py**
Tool name: `reply_gmail`
Input: `thread_id: str, message_id: str, body: str`
Fetches original to get recipients and subject, creates reply MIME, sends in thread
Returns: confirmation

**Story 5.1.5 — label_email.py**
Tool name: `label_gmail`
Input: `message_id: str, add_labels: list[str] = None, remove_labels: list[str] = None`
Calls `gmail_service.users().messages().modify()`
Returns: updated labels

**Story 5.1.6 — trash_email.py**
Tool name: `trash_gmail`
Input: `message_id: str`
Calls `gmail_service.users().messages().trash()`
Returns: confirmation

**Story 5.1.7 — list_threads.py**
Tool name: `list_gmail_threads`
Input: `query: str = None, max_results: int = 10`
Returns thread list with latest message snippet per thread

### Epic 5.2 — Calendar tools

**Story 5.2.1 — list_events.py**
Tool name: `list_calendar_events`
Input: `start_date: str, end_date: str, calendar_id: str = 'primary'`
Google API: `calendar_service.events().list()` with timeMin/timeMax
Embed: title + description
Upsert to: `calendar_events`
Return: formatted event list with time, title, attendees, meet link

**Story 5.2.2 — create_event.py**
Tool name: `create_calendar_event`
Input: `title: str, start_datetime: str, end_datetime: str, attendees: list[str] = None, description: str = None, add_meet: bool = True`
Creates event body, if add_meet=True adds `conferenceData` with `createRequest`
Calls `calendar_service.events().insert(conferenceDataVersion=1)`
Returns: event ID, Meet link, confirmation

**Story 5.2.3 — update_event.py**
Tool name: `update_calendar_event`
Input: `event_id: str, title: str = None, start_datetime: str = None, end_datetime: str = None, description: str = None`
Fetches existing event, patches only provided fields
Returns: updated event summary

**Story 5.2.4 — delete_event.py**
Tool name: `delete_calendar_event`
Input: `event_id: str, calendar_id: str = 'primary'`
Returns: confirmation

**Story 5.2.5 — check_availability.py**
Tool name: `check_calendar_availability`
Input: `start_datetime: str, end_datetime: str, attendee_emails: list[str] = None`
Uses `calendar_service.freebusy().query()` to find free/busy slots
Returns: availability summary for each attendee

### Epic 5.3 — Drive tools

**Story 5.3.1 — search_files.py**
Tool name: `search_drive`
Input: `query: str, mime_type: str = None, max_results: int = 10`
Google API: `drive_service.files().list()` with fullText search
Embed: file name + extracted content
Upsert to: `drive_documents`
Return: file list with id, name, type, link, modified date

**Story 5.3.2 — get_file.py**
Tool name: `get_drive_file`
Input: `file_id: str`
Fetches file metadata and exports content (for Docs/Sheets exports as plain text)
Returns: full content

**Story 5.3.3 — upload_file.py**
Tool name: `upload_drive_file`
Input: `file_path: str, parent_folder_id: str = None, name: str = None`
Uses `MediaFileUpload`, calls `drive_service.files().create()`
Returns: file ID and web link

**Story 5.3.4 — share_file.py**
Tool name: `share_drive_file`
Input: `file_id: str, email: str, role: str = 'reader'`
Creates permission via `drive_service.permissions().create()`
Returns: confirmation

**Story 5.3.5 — move_file.py**
Tool name: `move_drive_file`
Input: `file_id: str, new_folder_id: str`
Updates parents via `drive_service.files().update()`
Returns: confirmation

### Epic 5.4 — Docs tools

**Story 5.4.1 — read_doc.py**
Tool name: `read_google_doc`
Input: `document_id: str`
Calls `docs_service.documents().get()`, extracts plain text from content array
Embeds and upserts to `drive_documents`
Returns: full document text

**Story 5.4.2 — create_doc.py**
Tool name: `create_google_doc`
Input: `title: str, content: str = None`
Creates doc, if content provided inserts text via batchUpdate
Returns: document ID and link

**Story 5.4.3 — append_to_doc.py**
Tool name: `append_to_google_doc`
Input: `document_id: str, content: str`
Uses `docs_service.documents().batchUpdate()` with insertText at end of body
Returns: confirmation

### Epic 5.5 — Sheets tools

**Story 5.5.1 — read_sheet.py**
Tool name: `read_google_sheet`
Input: `spreadsheet_id: str, range: str = 'Sheet1'`
Calls `sheets_service.spreadsheets().values().get()`
Returns: data as formatted table string

**Story 5.5.2 — write_sheet.py**
Tool name: `write_google_sheet`
Input: `spreadsheet_id: str, range: str, values: list[list]`
Calls `sheets_service.spreadsheets().values().update(valueInputOption='USER_ENTERED')`
Returns: updated cell count

**Story 5.5.3 — append_rows.py**
Tool name: `append_to_google_sheet`
Input: `spreadsheet_id: str, values: list[list], sheet_name: str = 'Sheet1'`
Calls `sheets_service.spreadsheets().values().append()`
Returns: confirmation

**Story 5.5.4 — create_sheet.py**
Tool name: `create_google_sheet`
Input: `title: str`
Calls `sheets_service.spreadsheets().create()`
Returns: spreadsheet ID and link

### Epic 5.6 — Tasks tools

**Story 5.6.1 — list_tasks.py**
Tool name: `list_tasks`
Input: `tasklist_id: str = '@default', show_completed: bool = False`
Returns: task list with title, due date, status

**Story 5.6.2 — create_task.py**
Tool name: `create_task`
Input: `title: str, notes: str = None, due_date: str = None, tasklist_id: str = '@default'`
Returns: task ID and confirmation

**Story 5.6.3 — complete_task.py**
Tool name: `complete_task`
Input: `task_id: str, tasklist_id: str = '@default'`
Updates status to 'completed'
Returns: confirmation

### Epic 5.7 — Contacts tools

**Story 5.7.1 — search_contacts.py**
Tool name: `search_contacts`
Input: `query: str, max_results: int = 10`
Calls `people_service.people().searchContacts()` with personFields
Embed: displayName + email + organization
Upsert to: `contacts`
Returns: contact list with name, email, phone, org

**Story 5.7.2 — get_contact.py**
Tool name: `get_contact`
Input: `email: str`
Searches contacts by email address
Returns: full contact details

### Epic 5.8 — Chat tools

**Story 5.8.1 — list_spaces.py**
Tool name: `list_chat_spaces`
Input: none
Returns: list of Chat spaces/rooms the user is in

**Story 5.8.2 — send_message.py**
Tool name: `send_chat_message`
Input: `space_id: str, text: str`
Calls `chat_service.spaces().messages().create()`
Returns: confirmation

**Story 5.8.3 — resolve_destination.py (implemented reliability addendum)**
Tool name: `resolve_chat_destination`
Input: `destination: str`
Accepts a verified `spaces/...` resource, exact accessible display name, or direct-
message email. For email, call `spaces.findDirectMessage`; only its specific missing-DM
404 may call idempotent `spaces.setup`. Read back the returned space and bind its
`spaces/...` name into `send_chat_message`. This ordered write contract requires
`chat.spaces.create`; existing users reconnect once after the scope is deployed.
For the Google discovery client, include the deterministic `requestId` in the
`SetupSpaceRequest` body—not as a `spaces.setup()` keyword. After the final required
write tool succeeds, return directly to deterministic postcondition verification;
do not require an additional LLM turn to declare the write complete.

Google Chat writes also require a saved Chat API Configuration containing the app
identity; merely enabling `chat.googleapis.com` is insufficient. Classify `Google Chat
app not found` as incomplete project configuration and surface the actionable
sanitized cause. When an approved Calendar create already has start, duration,
timezone, attendees, and Meet intent, project those typed arguments and execute the
idempotent Calendar tool without another planning-model call.

---

## SPRINT 6 — FastAPI Backend

### Epic 6.1 — Database connection pool

**Story 6.1.1 — Create connection.py**
File: `app/db/connection.py`
```python
import asyncpg
from app.config.settings import get_settings

_pool = None

async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        settings = get_settings()
        _pool = await asyncpg.create_pool(
            settings.async_database_url.replace("+asyncpg", ""),
            min_size=2,
            max_size=10
        )
    return _pool

async def close_pool():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
```

### Epic 6.2 — FastAPI application

**Story 6.2.1 — Create main.py**
File: `app/api/main.py`

Must include:
- Lifespan context manager that initialises asyncpg pool and APScheduler on startup
- CORS middleware configured for localhost:3000 (Next.js) and Flutter
- Include routers for chat, feedback, history, admin
- Mount Prometheus metrics at `/metrics`
- Health check endpoint at `/health`
- LangSmith environment variables loaded from settings

**Story 6.2.2 — Create chat route with SSE streaming**
File: `app/api/routes/chat.py`

`POST /chat` endpoint must:
- Accept: `{message: str, session_id: str}`
- Load correct prompt via prompt_service (handles A/B assignment)
- Pass message + session to LangGraph agent
- Stream tokens back as SSE (`text/event-stream`)
- Each SSE event format: `data: {"token": "...", "done": false}\n\n`
- Final event: `data: {"token": "", "done": true, "session_id": "..."}\n\n`
- Record metric to prompt_metrics table (fire-and-forget)

```python
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
import asyncio, json

router = APIRouter()

@router.post("/chat")
async def chat(req: ChatRequest):
    async def event_stream():
        async for chunk in agent_graph.astream(
            {"message": req.message, "session_id": req.session_id}
        ):
            token = chunk.get("output", "")
            yield f"data: {json.dumps({'token': token, 'done': False})}\n\n"
        yield f"data: {json.dumps({'token': '', 'done': True})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
```

**Story 6.2.3 — Create feedback route**
File: `app/api/routes/feedback.py`

`POST /feedback` endpoint:
- Accept: `{session_id: str, rating: int}` where rating is +1 or -1
- Write to feedback table linking to most recent conversation turn
- Return: `{status: "recorded"}`

**Story 6.2.4 — Create history route**
File: `app/api/routes/history.py`

`GET /history/{session_id}` endpoint:
- Returns last 50 messages for a session ordered by created_at
- Returns: `{messages: [{role, content, created_at}]}`

**Story 6.2.5 — Create admin routes**
File: `app/api/routes/admin.py`

Endpoints:
- `GET /admin/experiments/{name}/summary` — returns experiment_summary view data
- `POST /admin/experiments` — creates new A/B experiment
- `POST /admin/experiments/{name}/conclude` — concludes experiment, promotes winner
- `GET /admin/prompts` — lists all prompts with versions
- `POST /admin/prompts` — creates new prompt version

**Story 6.2.6 — Prometheus metrics middleware**
File: `app/mlops/metrics.py`

Define these metrics:
```python
from prometheus_client import Counter, Histogram

tool_errors    = Counter('agent_tool_errors_total', 'Tool errors', ['tool_name'])
tool_latency   = Histogram('agent_tool_latency_seconds', 'Tool latency', ['tool_name'])
llm_latency    = Histogram('agent_llm_latency_seconds', 'LLM latency', ['model'])
empty_context  = Counter('agent_empty_context_total', 'Empty RAG retrievals')
request_count  = Counter('agent_requests_total', 'Total requests', ['endpoint'])
```

**Story 6.2.7 — JWT auth middleware**
File: `app/api/middleware/auth.py`

Simple JWT middleware:
- `POST /auth/token` endpoint accepts `{email: str}` and returns a JWT
- All `/chat`, `/feedback`, `/history` routes require `Authorization: Bearer <token>`
- Decode JWT to get user_id, inject into request state
- Admin routes require additional `admin: true` claim in JWT

**Story 6.2.8 — Verify FastAPI runs**
```bash
cd /Users/achintyatyagi/google-connector-app
source .venv/bin/activate
uvicorn app.api.main:app --reload --port 8000
```
In a second terminal:
```bash
curl http://localhost:8000/health
```
Must return: `{"status": "ok"}`

---

## SPRINT 7 — LangGraph Agent Graph

### Epic 7.1 — Agent state

**Story 7.1.1 — Create state.py**
File: `app/agents/state.py`

```python
from typing import TypedDict, Annotated, List
from langchain_core.messages import BaseMessage
import operator

class AgentState(TypedDict):
    messages: Annotated[List[BaseMessage], operator.add]
    session_id: str
    user_id: str
    current_tool: str
    tool_results: list
    retrieved_context: str
    model_to_use: str     # 'groq' or 'deepseek'
    error: str
    task_complete: bool
```

### Epic 7.2 — Model router

**Story 7.2.1 — Create router.py**
File: `app/agents/router.py`

Logic:
- If task involves: writing a long document, complex analysis, multi-step planning with ambiguity, deep reasoning → use `deepseek`
- If task involves: quick tool calls, Gmail search, Calendar lookup, simple writes → use `groq`
- Implemented as a LangGraph node that classifies the task and sets `state["model_to_use"]`

```python
from langchain_groq import ChatGroq
from langchain_community.chat_models import ChatDeepSeek

def get_llm(model_choice: str):
    if model_choice == "deepseek":
        return ChatDeepSeek(
            model="deepseek-reasoner",
            api_key=settings.deepseek_api_key
        )
    return ChatGroq(
        model="llama-3.3-70b-versatile",
        api_key=settings.groq_api_key,
        temperature=0.3
    )
```

### Epic 7.3 — Supervisor and subagents

**Story 7.3.1 — Create supervisor.py**
File: `app/agents/supervisor.py`

The supervisor node must:
- Receive the user message
- Classify it into one or more Google service categories
- Retrieve relevant context from pgvector via RAG retriever
- Inject retrieved context into system message
- Route to the appropriate subgraph(s)
- Handle multi-step commands that span multiple services
- Return final response to user

**Story 7.3.2 — Create LangGraph graph**
The complete graph must have:
- `route_model` node — decides Groq vs DeepSeek
- `retrieve_context` node — RAG retrieval
- `supervisor` node — task classification and routing
- One subgraph per Google service (gmail, calendar, drive, docs, sheets, tasks, chat, contacts)
- `error_handler` node — catches failures, logs to task_log, returns user-friendly error
- `respond` node — formats final response
- Postgres checkpointer for state persistence across turns
- Conditional edges from supervisor to each subgraph based on classification
- All subgraphs connect back to `respond` node

```python
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.postgres import PostgresSaver

def build_agent_graph(pool):
    checkpointer = PostgresSaver(pool)
    graph = StateGraph(AgentState)

    # Add all nodes
    graph.add_node("route_model", route_model_node)
    graph.add_node("retrieve_context", retrieve_context_node)
    graph.add_node("supervisor", supervisor_node)
    graph.add_node("gmail_agent", gmail_subgraph)
    graph.add_node("calendar_agent", calendar_subgraph)
    graph.add_node("drive_agent", drive_subgraph)
    graph.add_node("docs_agent", docs_subgraph)
    graph.add_node("sheets_agent", sheets_subgraph)
    graph.add_node("tasks_agent", tasks_subgraph)
    graph.add_node("chat_agent", chat_subgraph)
    graph.add_node("contacts_agent", contacts_subgraph)
    graph.add_node("error_handler", error_handler_node)
    graph.add_node("respond", respond_node)

    # Entry point
    graph.set_entry_point("route_model")

    # Edges
    graph.add_edge("route_model", "retrieve_context")
    graph.add_edge("retrieve_context", "supervisor")
    graph.add_conditional_edges("supervisor", route_to_subagent, {
        "gmail": "gmail_agent",
        "calendar": "calendar_agent",
        "drive": "drive_agent",
        "docs": "docs_agent",
        "sheets": "sheets_agent",
        "tasks": "tasks_agent",
        "chat": "chat_agent",
        "contacts": "contacts_agent",
        "error": "error_handler",
    })
    for agent in ["gmail_agent","calendar_agent","drive_agent",
                  "docs_agent","sheets_agent","tasks_agent",
                  "chat_agent","contacts_agent","error_handler"]:
        graph.add_edge(agent, "respond")
    graph.add_edge("respond", END)

    return graph.compile(checkpointer=checkpointer)
```

---

## SPRINT 8 — RAG Pipeline

### Epic 8.1 — Embedder

**Story 8.1.1 — Install Ollama and nomic-embed-text**
```bash
brew install ollama
ollama pull nomic-embed-text
ollama serve &
```
Verify:
```bash
curl http://localhost:11434/api/embeddings \
  -d '{"model": "nomic-embed-text", "prompt": "test"}'
```
Must return a JSON object with an `embedding` array of 768 floats.

**Story 8.1.2 — Create embedder.py**
File: `app/rag/embedder.py`

```python
import ollama
from langchain_community.embeddings import OllamaEmbeddings

class NomicEmbedder:
    def __init__(self):
        self.model = "nomic-embed-text"
        self.langchain_embedder = OllamaEmbeddings(model=self.model)

    async def aembed_query(self, text: str) -> list[float]:
        response = ollama.embeddings(model=self.model, prompt=text[:8000])
        return response["embedding"]

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        return [await self.aembed_query(t) for t in texts]
```

### Epic 8.2 — Hybrid retriever

**Story 8.2.1 — Create retriever.py**
File: `app/rag/retriever.py`

Must implement `hybrid_retrieve(query, pool, filters=None, top_k=5)`:
- Embed the query using NomicEmbedder
- Run parallel pgvector similarity search across `gmail_messages`, `drive_documents`, `contacts`, `chat_messages`
- For each table: `ORDER BY embedding <=> $1 LIMIT top_k`
- Optionally combine with SQL filters (date range, sender, label)
- Merge results, deduplicate, rank by combined score
- Return list of `{source, content, score, metadata}` dicts

Example hybrid query for gmail:
```sql
SELECT id, subject, body_plain, sender, received_at,
       1 - (embedding <=> $1) AS similarity
FROM gmail_messages
WHERE received_at > $2           -- optional date filter
ORDER BY embedding <=> $1
LIMIT $3;
```

**Story 8.2.2 — Create context_packer.py**
File: `app/rag/context_packer.py`

Takes retrieved docs list, formats them into a context string that fits within token budget (max 3000 tokens of context), prioritising highest-similarity results first.

### Epic 8.3 — Sync jobs

**Story 8.3.1 — Create gmail_sync.py**
File: `app/rag/sync/gmail_sync.py`

Must:
- Query `sync_log` for last successful gmail sync time
- Fetch emails modified after that time using Gmail API `q=after:{timestamp}`
- For each email: parse all fields, embed subject+body, upsert to `gmail_messages`
- Update `sync_log` with count and timestamp

**Story 8.3.2 — Create drive_sync.py**
File: `app/rag/sync/drive_sync.py`

Must:
- Use Drive Activity API to fetch files changed since last sync
- For each changed file: fetch content (export as plain text for Docs/Sheets), embed, upsert to `drive_documents`
- Update `sync_log`

**Story 8.3.3 — Create calendar_sync.py**
File: `app/rag/sync/calendar_sync.py`

Fetches events from last 30 days to next 90 days, upserts to `calendar_events`.

**Story 8.3.4 — Create contacts_sync.py**
File: `app/rag/sync/contacts_sync.py`

Fetches all contacts via People API, embeds displayName+email+org, upserts to `contacts`.

**Story 8.3.5 — Create scheduler.py**
File: `app/rag/sync/scheduler.py`

```python
from apscheduler.schedulers.asyncio import AsyncIOScheduler

scheduler = AsyncIOScheduler()

def setup_scheduler(pool, embedder):
    scheduler.add_job(gmail_sync,    'cron', hour=2,  minute=0,  args=[pool, embedder])
    scheduler.add_job(drive_sync,    'cron', hour=2,  minute=15, args=[pool, embedder])
    scheduler.add_job(calendar_sync, 'cron', hour=2,  minute=30, args=[pool, embedder])
    scheduler.add_job(contacts_sync, 'cron', hour=2,  minute=45, args=[pool, embedder])
    scheduler.start()
```

**Story 8.3.6 — Run initial full sync**
```bash
python3 -c "
import asyncio
from app.rag.sync.gmail_sync import gmail_sync
from app.rag.sync.drive_sync import drive_sync
asyncio.run(gmail_sync())
asyncio.run(drive_sync())
"
```
Verify rows appear in `gmail_messages` and `drive_documents` with non-null embeddings:
```bash
psql -U agent_user -d agent_db -c "SELECT COUNT(*) FROM gmail_messages WHERE embedding IS NOT NULL;"
```

---

## SPRINT 9 — MLOps Layer

### Epic 9.1 — LangSmith

**Story 9.1.1 — Sign up at smith.langchain.com**
Create free account. Get API key. Add to `.env`:
```
LANGCHAIN_TRACING_V2=true
LANGCHAIN_API_KEY=ls__xxx
LANGCHAIN_PROJECT=google-agent
```
LangGraph traces automatically when these env vars are set. No code changes needed.

### Epic 9.2 — RAGAS evaluation

**Story 9.2.1 — Create ragas_eval.py**
File: `scripts/run_ragas_eval.py`

Must:
- Connect to Postgres
- Pull 20 most recent `feedback` rows with rating = 1 (positive examples)
- Format as RAGAS dataset: question, answer, contexts, ground_truth
- Run `evaluate()` with faithfulness, answer_relevancy, context_recall
- Print scores and write to `prompt_metrics` table

**Story 9.2.2 — Create GitHub Action for weekly eval**
File: `.github/workflows/ragas_eval.yml`

Runs every Monday at 9am IST (3:30am UTC):
```yaml
on:
  schedule:
    - cron: '30 3 * * 1'
```

### Epic 9.3 — Prompt versioning

Already defined in schema (Sprint 2). Wire up `app/db/prompt_service.py` with:
- `get_prompt(name, session_id, model_target)` — returns resolved prompt with A/B assignment
- `record_metric(...)` — writes to prompt_metrics
- `create_experiment(...)` — creates A/B test
- `conclude_experiment(name, winner)` — promotes winner, deactivates loser

Full implementation from the prompt versioning spec discussed earlier in the project.

---

## SPRINT 10 — Next.js Web App

### Epic 10.1 — Project setup

**Story 10.1.1 — Create Next.js app**
```bash
cd /Users/achintyatyagi/google-connector-app
npx create-next-app@latest web --typescript --tailwind --eslint --app --src-dir --import-alias "@/*"
cd web
```

**Story 10.1.2 — Install additional dependencies**
```bash
npm install @google-cloud/local-auth googleapis axios eventsource
```

### Epic 10.2 — Pages and components

**Story 10.2.1 — Chat page**
File: `web/src/app/page.tsx`

Must implement:
- Full-screen chat UI with message history
- Input box at bottom with send button
- SSE connection to `POST http://localhost:8000/chat`
- Token-by-token streaming display (append each token as it arrives)
- Thumbs up / thumbs down buttons on each assistant message
- Session ID generated and persisted in localStorage
- Loading indicator while streaming
- Error state handling

**Story 10.2.2 — SSE client hook**
File: `web/src/hooks/useChat.ts`

```typescript
export function useChat(sessionId: string) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [streaming, setStreaming] = useState(false);

  const sendMessage = async (content: string) => {
    setStreaming(true);
    const response = await fetch('http://localhost:8000/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: content, session_id: sessionId })
    });

    const reader = response.body!.getReader();
    const decoder = new TextDecoder();
    let assistantMessage = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      const chunk = decoder.decode(value);
      const lines = chunk.split('\n\n');
      for (const line of lines) {
        if (line.startsWith('data: ')) {
          const data = JSON.parse(line.slice(6));
          if (!data.done) {
            assistantMessage += data.token;
            // Update last message in real time
          }
        }
      }
    }
    setStreaming(false);
  };

  return { messages, sendMessage, streaming };
}
```

**Story 10.2.3 — Feedback component**
File: `web/src/components/FeedbackButtons.tsx`

Thumbs up (+1) and thumbs down (-1) buttons. On click, POST to `/feedback` with session_id and rating. Show confirmation.

**Story 10.2.4 — Run and verify web app**
```bash
cd web && npm run dev
```
Open `http://localhost:3000`. Send a test message. Verify streaming response appears token by token.

---

## SPRINT 11 — Flutter Mobile App

### Epic 11.1 — Flutter setup

**Story 11.1.1 — Install Flutter**
```bash
brew install --cask flutter
flutter doctor
```
Resolve any issues flutter doctor reports before proceeding.

**Story 11.1.2 — Create Flutter project**
```bash
cd /Users/achintyatyagi/google-connector-app
flutter create mobile --org com.googleagent --platforms android,ios
cd mobile
```

**Story 11.1.3 — Install Flutter dependencies**
Add to `mobile/pubspec.yaml` under dependencies:
```yaml
dependencies:
  flutter:
    sdk: flutter
  http: ^1.2.0
  provider: ^6.1.0
  shared_preferences: ^2.2.0
  uuid: ^4.3.0
  flutter_markdown: ^0.6.18
  google_sign_in: ^6.2.0
```
Run: `flutter pub get`

### Epic 11.2 — Flutter screens

**Story 11.2.1 — Chat screen**
File: `mobile/lib/screens/chat_screen.dart`

Must implement:
- ListView of chat messages (user right-aligned, assistant left-aligned)
- Text input field with send button at bottom
- SSE streaming from FastAPI using `http` package with chunked response reading
- Each assistant message appends tokens as they arrive (setState per chunk)
- Thumbs up/down buttons per assistant message
- Session ID from SharedPreferences (generate UUID on first launch, persist)

**Story 11.2.2 — SSE service**
File: `mobile/lib/services/chat_service.dart`

```dart
import 'dart:async';
import 'dart:convert';
import 'package:http/http.dart' as http;

class ChatService {
  final String baseUrl = 'http://localhost:8000';

  Stream<String> sendMessage(String message, String sessionId) async* {
    final request = http.Request('POST', Uri.parse('$baseUrl/chat'));
    request.headers['Content-Type'] = 'application/json';
    request.body = jsonEncode({'message': message, 'session_id': sessionId});

    final response = await http.Client().send(request);
    await for (final chunk in response.stream.transform(utf8.decoder)) {
      for (final line in chunk.split('\n\n')) {
        if (line.startsWith('data: ')) {
          final data = jsonDecode(line.substring(6));
          if (data['done'] == false) {
            yield data['token'] as String;
          }
        }
      }
    }
  }

  Future<void> sendFeedback(String sessionId, int rating) async {
    await http.post(
      Uri.parse('$baseUrl/feedback'),
      headers: {'Content-Type': 'application/json'},
      body: jsonEncode({'session_id': sessionId, 'rating': rating}),
    );
  }
}
```

**Story 11.2.3 — Main app entry**
File: `mobile/lib/main.dart`

Sets up MaterialApp with ChatScreen as home. Dark and light theme support. Bottom navigation bar placeholder for future screens (History, Settings).

**Story 11.2.4 — Run and verify Flutter app**
```bash
cd mobile
flutter run
```
On iOS simulator or Android emulator. Send a test message. Verify streaming works.

---

## SPRINT 12 — Dockerfile and CI/CD

### Epic 12.1 — Docker

**Story 12.1.1 — Create Dockerfile**
File: `Dockerfile`

```dockerfile
FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    gcc libpq-dev && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

**Story 12.1.2 — Create docker-compose.yml**
File: `docker-compose.yml`

Services:
- `api` — builds from Dockerfile, port 8000
- `postgres` — postgres:16 image with pgvector, port 5432, volume for data persistence
- `ollama` — ollama/ollama image, port 11434, for local embedding

```yaml
version: '3.9'
services:
  api:
    build: .
    ports: ["8000:8000"]
    env_file: .env
    depends_on: [postgres, ollama]

  postgres:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_DB: agent_db
      POSTGRES_USER: agent_user
      POSTGRES_PASSWORD: agent_pass_2024
    ports: ["5432:5432"]
    volumes: [postgres_data:/var/lib/postgresql/data]

  ollama:
    image: ollama/ollama
    ports: ["11434:11434"]
    volumes: [ollama_data:/root/.ollama]

volumes:
  postgres_data:
  ollama_data:
```

### Epic 12.2 — GitHub Actions CI

**Story 12.2.1 — Create CI workflow**
File: `.github/workflows/ci.yml`

```yaml
name: CI

on:
  push:
    branches: [main, develop]
  pull_request:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - name: Install dependencies
        run: pip install -r requirements.txt
      - name: Run tests
        run: pytest tests/ -v
      - name: Lint
        run: pip install flake8 && flake8 app/ --max-line-length=100
```

**Story 12.2.2 — Create deployment workflow**
File: `.github/workflows/deploy.yml`

```yaml
name: Deploy

on:
  push:
    branches: [main]

jobs:
  deploy-backend:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Deploy to Railway
        uses: bervProject/railway-deploy@main
        with:
          railway_token: ${{ secrets.RAILWAY_TOKEN }}
          service: google-agent-api

  deploy-web:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: amondnet/vercel-action@v25
        with:
          vercel-token: ${{ secrets.VERCEL_TOKEN }}
          vercel-org-id: ${{ secrets.VERCEL_ORG_ID }}
          vercel-project-id: ${{ secrets.VERCEL_PROJECT_ID }}
          working-directory: ./web
```

---

## SPRINT 13 — Production Deployment

### Epic 13.1 — Neon Postgres (production DB)

**Story 13.1.1 — Create Neon account and project**
- Go to `neon.tech`, sign up free
- Create project named `google-agent-prod`
- Copy connection string
- Run all Alembic migrations against Neon DB:
```bash
DATABASE_URL=<neon_connection_string> alembic upgrade head
```

### Epic 13.2 — Railway (production backend)

**Story 13.2.1 — Deploy FastAPI to Railway**
- Sign up at `railway.app`
- Create new project from GitHub repo
- Set all environment variables from `.env` in Railway dashboard
- Set `DATABASE_URL` to Neon connection string
- Railway auto-detects Dockerfile and deploys
- Copy Railway deployment URL

**Story 13.2.2 — Keep-alive cron**
Add to scheduler:
```python
scheduler.add_job(ping_self, 'interval', minutes=10)
```
Where `ping_self` calls `GET {RAILWAY_URL}/health` to prevent sleep.

### Epic 13.3 — Vercel (production web)

**Story 13.3.1 — Deploy Next.js to Vercel**
```bash
cd web
npx vercel --prod
```
Set environment variable `NEXT_PUBLIC_API_URL` to Railway backend URL.
Update all `fetch` calls in Next.js to use `process.env.NEXT_PUBLIC_API_URL`.

### Epic 13.4 — Final push

**Story 13.4.1 — Commit everything and push**
```bash
cd /Users/achintyatyagi/google-connector-app
git add .
git commit -m "Complete implementation — all phases done"
git push origin main
```

Verify GitHub Actions CI passes. Verify Railway deploys successfully. Verify Vercel deploys successfully.

---

## Verification Checklist — Run Before Marking Complete

### Backend
```bash
curl https://<railway-url>/health
# → {"status": "ok"}

curl -X POST https://<railway-url>/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "search my emails about meetings", "session_id": "test-123"}'
# → SSE stream with tokens
```

### Database
```bash
psql -U agent_user -d agent_db -c "SELECT COUNT(*) FROM gmail_messages WHERE embedding IS NOT NULL;"
# → count > 0

psql -U agent_user -d agent_db -c "SELECT name, version, is_active FROM prompts;"
# → 2 rows, v1 active
```

### RAG
```bash
python3 -c "
from app.rag.retriever import hybrid_retrieve
import asyncio
results = asyncio.run(hybrid_retrieve('budget meeting'))
print(f'{len(results)} results returned')
print(results[0])
"
# → at least 1 result with content and similarity score
```

### Web app
Open `https://<vercel-url>` in browser. Send message. Verify SSE streaming works.

### Flutter
Run `flutter run` on simulator. Send message. Verify streaming and feedback buttons work.

---

## Environment Variables Reference

```bash
# LLM APIs
GROQ_API_KEY=gsk_xxx
DEEPSEEK_API_KEY=sk-xxx

# Database
DATABASE_URL=postgresql://agent_user:agent_pass_2024@localhost:5432/agent_db
ASYNC_DATABASE_URL=postgresql+asyncpg://agent_user:agent_pass_2024@localhost:5432/agent_db

# Observability
LANGCHAIN_TRACING_V2=true
LANGCHAIN_API_KEY=ls__xxx
LANGCHAIN_PROJECT=google-agent

# Google
GOOGLE_CREDENTIALS_PATH=./credentials.json

# Auth
JWT_SECRET_KEY=change-this-in-production-use-256-bit-random-string
JWT_ALGORITHM=HS256

# Production only
RAILWAY_URL=https://xxx.railway.app
NEON_DATABASE_URL=postgresql://xxx.neon.tech/neondb
```

---

## Notes for Codex

1. Always activate the venv before running Python: `source .venv/bin/activate`
2. Use `python3` not `python` on this Mac
3. The `credentials.json` and `token.pkl` already exist and are working — do not delete or regenerate them
4. The Google account authorised is `achintyat256@gmail.com`
5. Run each sprint's verification steps before moving to the next sprint
6. If a pip install fails due to a package not found, search PyPI for the correct package name and install the closest equivalent
7. For any LangGraph or LangChain API that has changed, check the latest docs at docs.langchain.com and adapt accordingly
8. Do not hardcode any API keys — always read from environment variables via `get_settings()`
9. All async database operations use asyncpg — do not mix with synchronous psycopg2 calls in async contexts
10. The pgvector HNSW index parameters `m=16, ef_construction=64` are correct for the expected data size — do not change them
## Addendum — Service-wide durable hybrid execution

All registered Workspace services use a shared execution selection policy. A
single-tool operation with complete schema-valid arguments executes through a typed
adapter without spending model tokens. Ordered composites use explicit contracts and
lineage. If arguments are incomplete or the operation genuinely needs planning, the
worker may select the bounded service agent before making any external call.

Deterministic failure never means “let the model try the write again.” After a tool
attempt, failures and uncertain results enter verification/reconciliation and durable
resume rules. Typed and agent paths have the same approval hash, authorization,
idempotency, projection, postconditions, artifact ledger, incident generation and
tenant isolation. Approval UI must preview sanitized concrete actions and must never
display complete private message/document content.

## Addendum — Active-session timing and model accounting

The main session progress card must show total elapsed request time and recorded step
time for both active and terminal durable runs. It must also show input/output/total
token use grouped by the actual LLM recorded in `agent_model_calls`, including
fallbacks. A deterministic run with no model calls must explicitly display zero LLM
tokens, while historical aggregate-only records must be labelled as such.

## Addendum — Conversation-context authority and composition delivery

Conversation history is selectively loaded only for a current-turn reference or
service clarification. It is data, never execution authority: only services and write
intent present in the current statement may enter the durable plan. Ordinary words
such as `space` must not imply Google Chat, while natural delivery forms including
`sendchat`, `send chat`, and `send on Google Chat` must be normalized consistently.

A combined content-generation and delivery request creates an ordered composition
dependency followed by the authorized Workspace write. A Chat write binds either the
completed composition output, an explicitly referenced prior assistant result, or
another verified dependency as exact typed content. It then uses the existing
destination-resolution, approval, idempotency, verification, and no-blind-retry
contract without another model planning call. Structured backend failures must be
decoded into readable messages by clients and must never render as `[object Object]`.
