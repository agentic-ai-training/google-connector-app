"""Real flock/SQLite/process-death tests; no image load or approval fabricated."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from app.coding import isolated_validation as runner


def intent(**changes):
    return {"version": 1, "role": "react", "host_instance_id": "822115653245959101",
            "image_id": "sha256:" + "a" * 64, "archive_sha256": "b" * 64,
            "archive_bytes": 108749312, "recipe_sha256": "c" * 64,
            "policy_sha256": "d" * 64, "registry_sha256": "e" * 64,
            "stop_unix": int(time.time()) + 3600, **changes}


def advance(journal, item, phase, evidence="f" * 64):
    return journal.record_toolchain_import(item["id"], item["phase"], phase, evidence)


def test_existing_job_prevents_import_and_import_prevents_both_job_purposes(tmp_path):
    with runner.Journal(tmp_path) as journal:
        job = journal.begin("python_compile", "sha256:" + "a" * 64, 1)
        with pytest.raises(runner.IsolationError, match="unreconciled job"):
            journal.begin_toolchain_import(intent())
        journal.record(job, "cleaned")
        journal.begin_toolchain_import(intent())
        with pytest.raises(runner.IsolationError, match="toolchain import"):
            journal.begin("python_compile", "sha256:" + "a" * 64, 1)
        from uuid import uuid4
        binding = {"build_job_id": str(uuid4()), "request_hash": "a" * 64,
                   "execution_deadline_unix": int(time.time()) + 30}
        with pytest.raises(runner.IsolationError, match="toolchain import"):
            journal.begin("image_packaging", "sha256:" + "a" * 64, 1,
                          binding, purpose="image_packaging")


@pytest.mark.parametrize("phase", ["prepared", "archive_verified", "load_started", "load_uncertain"])
def test_abrupt_process_death_preserves_intent_without_replaying_or_expiring(tmp_path, phase):
    request = intent()
    code = "\n".join([
        "import json,os,pathlib,sys",
        "from app.coding.isolated_validation import Journal",
        "with Journal(pathlib.Path(sys.argv[1])) as journal:",
        " item=journal.begin_toolchain_import(json.loads(sys.argv[2]))",
        " for phase in json.loads(sys.argv[3]):",
        "  item=journal.record_toolchain_import(item['id'],item['phase'],phase,'f'*64)",
        " os._exit(23)",
    ])
    order = ["prepared", "archive_verified", "load_started", "load_uncertain"]
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path), json.dumps(request),
                             json.dumps(order[1:order.index(phase) + 1])],
                            capture_output=True, timeout=15, check=False)
    assert result.returncode == 23, result.stderr
    with runner.Journal(tmp_path) as journal:
        recovered = journal.begin_toolchain_import(request)
        assert recovered["phase"] == phase and recovered["image_approved"] is False
        assert len(journal.toolchain_imports()) == 1
        with pytest.raises(runner.IsolationError, match="toolchain import"):
            journal.require_available()


def test_shared_flock_excludes_second_import_owner(tmp_path):
    with runner.Journal(tmp_path) as journal:
        journal.begin_toolchain_import(intent())
        with pytest.raises(BlockingIOError):
            with runner.Journal(tmp_path):
                pytest.fail("Second importer obtained the same host slot")


@pytest.mark.parametrize("phase", ["prepared", "archive_verified"])
def test_explicit_preload_cancellation_releases_slot_and_retains_history(tmp_path, phase):
    with runner.Journal(tmp_path) as journal:
        item = journal.begin_toolchain_import(intent())
        if phase != "prepared":
            item = advance(journal, item, phase)
        cancelled = advance(journal, item, "cancelled")
        journal.require_available()
        assert cancelled["phase"] == "cancelled" and not cancelled["image_approved"]
        assert journal.begin_toolchain_import(item["intent"]) == cancelled


def test_lost_load_observation_requires_reconciliation_not_cancel_or_duplicate_load(tmp_path):
    with runner.Journal(tmp_path) as journal:
        item = journal.begin_toolchain_import(intent())
        item = advance(journal, item, "archive_verified")
        item = advance(journal, item, "load_started")
        item = advance(journal, item, "load_uncertain")
        for phase in ("cancelled", "load_started", "committed"):
            with pytest.raises(runner.IsolationError, match="not permitted"):
                advance(journal, item, phase)
        item = advance(journal, item, "loaded")
        item = advance(journal, item, "proof_pending")
        with pytest.raises(runner.IsolationError, match="toolchain import"):
            journal.require_available()
        assert item["image_approved"] is False


def test_observation_retry_is_idempotent_but_changed_evidence_or_phase_is_not(tmp_path):
    with runner.Journal(tmp_path) as journal:
        item = journal.begin_toolchain_import(intent())
        advanced = advance(journal, item, "archive_verified")
        assert advance(journal, item, "archive_verified") == advanced
        with pytest.raises(runner.IsolationError, match="phase changed"):
            advance(journal, item, "archive_verified", "a" * 64)


def test_cancelled_import_only_reopens_same_previously_verified_receipt(tmp_path, monkeypatch):
    with runner.Journal(tmp_path) as journal:
        item = journal.begin_toolchain_import(intent())
        verified = advance(journal, item, 'archive_verified')
        cancelled = advance(journal, verified, 'cancelled', 'a' * 64)
        with pytest.raises(runner.IsolationError, match='exact unexpired'):
            advance(journal, cancelled, 'archive_verified', 'b' * 64)
        job = journal.begin('python_compile', 'sha256:' + 'a' * 64, 1)
        with pytest.raises(runner.IsolationError, match='unreconciled job'):
            advance(journal, cancelled, 'archive_verified')
        journal.record(job, 'cleaned')
        reopened = advance(journal, cancelled, 'archive_verified')
        assert advance(journal, cancelled, 'archive_verified') == reopened
        cancelled = advance(journal, reopened, 'cancelled', 'a' * 64)
        monkeypatch.setattr(runner.time, 'time', lambda: item['intent']['stop_unix'] + 1)
        with pytest.raises(runner.IsolationError, match='exact unexpired'):
            advance(journal, cancelled, 'archive_verified')


def test_cancelled_prepared_without_verified_receipt_cannot_reopen(tmp_path):
    with runner.Journal(tmp_path) as journal:
        cancelled = advance(journal, journal.begin_toolchain_import(intent()), 'cancelled')
        with pytest.raises(runner.IsolationError, match='exact unexpired'):
            advance(journal, cancelled, 'archive_verified')


def test_expiry_blocks_load_but_never_clears_ambiguous_state(tmp_path, monkeypatch):
    with runner.Journal(tmp_path) as journal:
        request = intent()
        item = journal.begin_toolchain_import(request)
        item = advance(journal, item, "archive_verified")
        monkeypatch.setattr(runner.time, "time", lambda: request["stop_unix"] + 1)
        with pytest.raises(runner.IsolationError, match="expired"):
            advance(journal, item, "load_started")
        assert journal.begin_toolchain_import(request)["phase"] == "archive_verified"
        with pytest.raises(runner.IsolationError, match="toolchain import"):
            journal.require_available()
        advance(journal, item, "cancelled")
        with pytest.raises(runner.IsolationError, match="expired"):
            journal.begin_toolchain_import(intent(role="spring", stop_unix=request["stop_unix"]))


@pytest.mark.parametrize("changes", [{"command": "docker load"}, {"role": []}, {"role": "python"},
                                    {"archive_bytes": True}, {"archive_bytes": 536870913},
                                    {"host_instance_id": "../target"}, {"registry_sha256": "secret"},
                                    {"image_id": "tag:latest"}, {"version": True}])
def test_intent_cannot_carry_arbitrary_execution_or_unbounded_custody(tmp_path, changes):
    with runner.Journal(tmp_path) as journal:
        with pytest.raises(runner.IsolationError):
            journal.begin_toolchain_import(intent(**changes))
        assert journal.toolchain_imports() == []


@pytest.mark.parametrize("column,value", [("phase", "committed"), ("intent", "{}"),
                                         ("observations", "[]"), ("updated_at", 0)])
def test_corrupt_import_never_makes_host_available(tmp_path, column, value):
    with runner.Journal(tmp_path) as journal:
        journal.begin_toolchain_import(intent())
        # Fixed test-only column names, never a runtime SQL tool.
        with journal.db:
            journal.db.execute("UPDATE toolchain_imports SET " + column + "=?", (value,))
        with pytest.raises(runner.IsolationError):
            journal.require_available()


def test_full_audit_stops_without_automatic_pruning(tmp_path):
    with runner.Journal(tmp_path) as journal:
        for index in range(8):
            item = journal.begin_toolchain_import(intent(recipe_sha256=f"{index:064x}"))
            advance(journal, item, "cancelled")
        with pytest.raises(runner.IsolationError, match="history"):
            journal.begin_toolchain_import(intent(recipe_sha256="f" * 64))
        assert len(journal.toolchain_imports()) == 8
