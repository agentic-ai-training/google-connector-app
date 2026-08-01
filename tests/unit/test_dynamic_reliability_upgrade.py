from unittest.mock import MagicMock

from app.improvements.builder import _execute_builder_repository_tool
from app.improvements.builder_tools import BoundedRepositoryTools
from app.improvements.candidates import validate_candidate_files
from app.evaluation.metrics import evaluate_plan
from app.runs.informational import workspace_chat_answer
from app.runs.planner import build_plan, classify_request
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
