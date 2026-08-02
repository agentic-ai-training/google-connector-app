import json
from pathlib import Path
from unittest.mock import MagicMock

from app.improvements.builder import (
    BUILDER_413_RETRY_MAX_CHARS,
    BUILDER_AUTHOR_EARLY_FILE_ROUND,
    BUILDER_AUTHOR_HARD_FILE_ROUND,
    BUILDER_AUTHOR_RESTRICTED_ROUND,
    _candidate_grounding_bundle,
    _candidate_prompt,
    _restricted_builder_schemas,
    _execute_builder_repository_tool,
    candidate_build_admission,
    normalize_candidate_incident,
)
from app.improvements.builder_tools import BoundedRepositoryTools
from app.improvements.candidates import validate_candidate_files
from app.evaluation.metrics import evaluate_plan
from app.runs.informational import workspace_chat_answer
from app.runs.planner import (
    CALENDAR_DURATION_QUESTION,
    CALENDAR_END_DATE_QUESTION,
    CALENDAR_RECURRENCE_QUESTION,
    CALENDAR_START_DATE_QUESTION,
    CALENDAR_START_TIME_QUESTION,
    CALENDAR_TIMEZONE_QUESTION,
    build_plan,
    classify_request,
)
from app.runs.verifier import _calendar_verification
from app.runs import verifier
from app.tools import registry


def test_dynamic_gmail_sender_language_maps_to_metadata_workflows():
    plan, _ = build_plan(
        "Give me the names of the last 20 people who sent promotional mails "
        "today in Asia/Kolkata"
    )
    step = plan.steps[0]
    assert (step.service, step.operation) == ("gmail", "recent_senders")
    assert step.arguments["tool_arguments"] == {
        "max_results": 20,
        "query": "-in:sent",
        "unique": True,
        "category": "promotions",
        "period": "today",
        "timezone": "Asia/Kolkata",
    }

    count_plan, _ = build_plan(
        "How many senders sent me promotional mails today in Asia/Kolkata?"
    )
    count_step = count_plan.steps[0]
    assert count_step.operation == "sender_count"
    assert count_step.arguments["allowed_tools"] == ["count_gmail_senders"]
    assert count_step.arguments["tool_arguments"]["category"] == "promotions"


def test_calendar_synonyms_and_recurrence_are_structured_before_execution():
    policy = classify_request("Add a recurring event every week at 10 AM")
    assert policy["services"] == ["calendar"]
    assert policy["intent_kind"] == "workspace_action"
    assert "On what date should the recurrence start?" in policy["required_clarifications"]
    assert "When should the recurrence end?" in policy["required_clarifications"]


def test_recurring_calendar_uses_typed_answers_without_repeating_prose():
    message = (
        "create a recurring calender event for me for the next 10 years "
        "to brush my teeth"
    )
    answers = {
        CALENDAR_START_TIME_QUESTION: "10:00 AM",
        CALENDAR_DURATION_QUESTION: "10 minutes",
        CALENDAR_TIMEZONE_QUESTION: "Asia/Kolkata",
        CALENDAR_RECURRENCE_QUESTION: "daily",
        CALENDAR_START_DATE_QUESTION: "2030-08-01",
        CALENDAR_END_DATE_QUESTION: "10 years later",
    }
    plan, policy = build_plan(message, clarification_answers=answers)

    assert policy["required_clarifications"] == []
    assert "User clarifications:" not in plan.objective
    arguments = plan.steps[0].arguments["tool_arguments"]
    assert arguments["start_datetime"] == "2030-08-01T10:00:00+05:30"
    assert arguments["duration_minutes"] == 10
    assert arguments["title"] == "Brush my teeth"
    assert arguments["recurrence"] == ["RRULE:FREQ=DAILY;UNTIL=20400801T235959Z"]


def test_past_typed_recurring_start_is_reasked_not_silently_changed():
    policy = classify_request(
        "create a recurring calendar event daily for the next 10 years",
        clarification_answers={
            CALENDAR_START_TIME_QUESTION: "10:00 AM",
            CALENDAR_DURATION_QUESTION: "10 minutes",
            CALENDAR_TIMEZONE_QUESTION: "Asia/Kolkata",
            CALENDAR_RECURRENCE_QUESTION: "daily",
            CALENDAR_START_DATE_QUESTION: "2020-08-01",
            CALENDAR_END_DATE_QUESTION: "2030-08-01",
        },
    )
    assert CALENDAR_START_DATE_QUESTION in policy["required_clarifications"]


def test_link_reference_without_a_url_requires_clarification():
    plan, policy = build_plan(
        "Send that link on Google Chat to person@example.com",
        referenced_output="This is a paragraph with no URL.",
    )
    assert "Which exact link should be used? The referenced content contains no URL." in (
        plan.required_clarifications
    )
    assert policy["required_clarifications"] == plan.required_clarifications


def test_disallowed_sexual_delivery_is_a_zero_provider_policy_decision():
    plan, policy = build_plan("Send nudes to person@example.com on Google Chat")
    assert policy["intent_kind"] == "policy_refusal"
    assert plan.estimated_max_tokens == 0
    assert plan.steps[0].arguments["allowed_tools"] == []
    assert evaluate_plan(plan, {
        "services": ["general"],
        "operations": ["answer_workspace_chat"],
        "max_tokens": 0,
    })["cost_efficiency"] == 1.0
    answer = workspace_chat_answer(
        "Send nudes to person@example.com on Google Chat", "policy_refusal", {},
    )
    assert "won’t initiate" in answer


def test_calendar_verifier_compares_instants_and_recurrence(monkeypatch):
    event = {
        "id": "event-1",
        "status": "confirmed",
        "start": {"dateTime": "2026-08-02T04:30:00Z", "timeZone": "Asia/Kolkata"},
        "end": {"dateTime": "2026-08-02T05:30:00Z", "timeZone": "Asia/Kolkata"},
        "recurrence": ["RRULE:FREQ=WEEKLY"],
    }
    execute = MagicMock(return_value=event)
    monkeypatch.setattr(verifier.google, "calendar_service", MagicMock(
        events=MagicMock(return_value=MagicMock(get=MagicMock(
            return_value=MagicMock(execute=execute),
        ))),
    ))
    passed, evidence = _calendar_verification("create_calendar_event", {
        "title": "Weekly review",
        "start_datetime": "2026-08-02T10:00:00+05:30",
        "end_datetime": "2026-08-02T11:00:00+05:30",
        "timezone": "Asia/Kolkata",
        "recurrence": ["RRULE:FREQ=WEEKLY"],
    }, {"id": "event-1"})
    assert passed is True
    assert evidence["start_match"] is True
    assert evidence["recurrence_match"] is True


def test_candidate_builder_rejects_placeholder_code_and_noop_tests(tmp_path):
    errors = validate_candidate_files([
        {
            "path": "app/runs_api.py",
            "change_type": "create",
            "content": "class RunsApi:\n    def load(self):\n        pass\n",
        },
        {
            "path": "tests/test_runs_api.py",
            "change_type": "create",
            "content": "def test_load():\n    pass\n",
        },
    ])
    assert any("placeholder-only" in item for item in errors)
    assert any("no assertion" in item for item in errors)

    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "runtime.py").write_text("def active():\n    return True\n")
    tools = BoundedRepositoryTools(tmp_path)
    rejected = _execute_builder_repository_tool(tools, "stage_candidate_file", {
        "path": "app/runtime.py",
        "change_type": "replace",
        "content": "def active():\n    return False\n",
    })
    assert rejected["error"] == "runtime_source_read_required"
    localized = tools.execute("localize_runtime_boundary", {
        "terms": ["active"], "service": "runs", "operation": "execute",
    })
    assert localized["matches"][0]["path"] == "app/runtime.py"
    tools.execute("read_repository_symbol", {
        "path": "app/runtime.py", "symbol": "active",
    })
    accepted = _execute_builder_repository_tool(tools, "stage_candidate_file", {
        "path": "app/runtime.py",
        "change_type": "replace",
        "content": "def active():\n    return False\n",
    })
    assert accepted["staged"] == "app/runtime.py"

    invalid = _execute_builder_repository_tool(tools, "stage_candidate_file", {
        "path": "app/runtime.py",
        "change_type": "replace",
        "content": "def active(:\n    return False\n",
    })
    assert invalid["error"] == "staged_candidate_repair_required"
    assert "candidate_syntax_invalid" in invalid["contract_errors"]
    assert invalid["retained_for_repair"] == "app/runtime.py"
    finding = invalid["validation"]["errors"][0]
    assert finding["line"] == 1
    assert finding["column"] is not None
    assert finding["error_type"] == "SyntaxError"
    assert "1: def active(:" in finding["context"]
    assert "app/runtime.py" in tools.staged
    repaired = _execute_builder_repository_tool(tools, "apply_candidate_patch", {
        "path": "app/runtime.py", "start_line": 1, "end_line": 2,
        "replacement": "def active():\n    return False",
    })
    assert repaired["staged"] == "app/runtime.py"
    assert tools.validate_staged()["valid"] is True


def test_candidate_admission_blocks_weak_diagnoses_and_derives_write_semantics():
    weak = {
        "title": "Runs API failure", "stage": "api", "category": "persistence",
        "component": "runs_api",
        "root_cause": "The current structured evidence is not specific enough for a safe change.",
        "evidence": {},
    }
    decision = candidate_build_admission(weak, {
        "automation_eligible": False,
    })
    assert decision["eligible"] is False
    assert decision["reason_codes"] == [
        "selected_strategy_requires_engineering_evidence",
        "specific_failure_evidence_required",
    ]

    normalized = normalize_candidate_incident({
        "operation": "create_calendar_event",
        "request_shape": {"write": False},
    })
    assert normalized["request_shape"]["write"] is True
    assert normalized["evidence_validation"]["corrections"] == [
        "request_shape_write_derived_from_operation",
    ]


def test_candidate_grounding_reads_real_runtime_and_test_paths(tmp_path):
    (tmp_path / "app" / "api" / "routes").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "app" / "api" / "routes" / "runs.py").write_text(
        "def create_durable_run():\n    return 'created'\n"
    )
    (tmp_path / "tests" / "test_runs.py").write_text(
        "def test_create_durable_run():\n    assert True\n"
    )
    tools = BoundedRepositoryTools(tmp_path)
    bundle = _candidate_grounding_bundle(tools, {
        "component": "runs_api", "service": "runs", "operation": "create",
        "stage": "api", "category": "persistence",
        "selected_option": {"change_scope": ["durable run"]},
    })
    assert "app/api/routes/runs.py" in tools.read_paths
    assert "tests/test_runs.py" in tools.read_paths
    assert bundle["sources"]


def test_calendar_grounding_fits_provider_fallback_history_budget():
    root = Path(__file__).resolve().parents[2]
    tools = BoundedRepositoryTools(root)
    incident = {
        "id": "calendar-postcondition-regression",
        "title": "Calendar clarification and postcondition failure",
        "stage": "verification",
        "category": "tool_failure",
        "component": "step_executor",
        "service": "calendar",
        "operation": "create_calendar_event",
        "root_cause": "Calendar write evidence did not satisfy the verifier",
        "breaking_point": "Execute and verify the calendar portion",
        "request_shape": {"write": True, "multi_service": False},
        "evidence": {"failure_code": "postcondition_failure"},
        "selected_option": {
            "id": "repair-calendar-runtime",
            "automation_eligible": True,
            "change_scope": ["calendar executor", "artifact verifier"],
        },
    }
    grounding = _candidate_grounding_bundle(tools, incident)
    job = {"sanitized_input": incident, "grounding_bundle": grounding}
    sources = [{
        "repository": "ephemeral checkout",
        "approved_roots": ["app/", "tests/", "docs/"],
        "read_limit_bytes": tools.max_read_bytes,
        "tool_call_limit": tools.max_calls,
        "changed_file_limit": tools.max_files,
    }, {"deterministic_repository_grounding": grounding}]
    messages = [{
        "role": "user",
        "content": _candidate_prompt(
            job, sources, "investigator_and_patch_author",
        ),
    }]

    assert len(json.dumps(messages)) <= BUILDER_413_RETRY_MAX_CHARS
    assert any(path.startswith("app/") for path in tools.read_paths)
    assert any(path.startswith("tests/") for path in tools.read_paths)


def test_grounded_author_is_staging_first_before_model_budget_is_spent(tmp_path):
    tools = BoundedRepositoryTools(tmp_path)
    names = {
        (schema.get("function") or {}).get("name")
        for schema in _restricted_builder_schemas(tools.schemas())
    }

    assert BUILDER_AUTHOR_EARLY_FILE_ROUND == 0
    assert BUILDER_AUTHOR_RESTRICTED_ROUND == 0
    assert BUILDER_AUTHOR_HARD_FILE_ROUND == 2
    assert "apply_candidate_patch" in names
    assert "stage_candidate_file" in names
    assert "validate_staged_candidate" in names
    assert "read_repository_file" not in names
    assert "search_repository" not in names
