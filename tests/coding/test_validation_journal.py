import json
import hashlib
import sqlite3
import time
from pathlib import Path
from uuid import uuid4

import pytest

from app.coding import isolated_validation as runner


IMAGE = "sha256:" + "a" * 64
CID = "b" * 64


class Engine:
    def __init__(self):
        self.containers = {}
        self.removed = []
        self.unavailable = False

    def add(self, job, scope, **changes):
        self.containers[CID] = {
            "Name": "/" + job["name"], "Image": job["image"],
            "Config": {"Labels": {
                "gca.purpose": job.get("purpose", "validation"), "gca.scope": scope, "gca.job": job["id"],
            }}, **changes,
        }

    def __call__(self, *args, **kwargs):
        if self.unavailable:
            raise runner.IsolationError("Docker unavailable")
        if args[0] == "ps":
            selected = args[args.index("--filter") + 1]
            if selected.startswith("name="):
                name = selected.removeprefix("name=^").removesuffix("$")
                return "\n".join(
                    key for key, value in self.containers.items() if value["Name"] == name
                ).encode()
            scope = selected.removeprefix("label=gca.scope=")
            return "\n".join(
                key + " " + value["Config"]["Labels"]["gca.job"]
                for key, value in self.containers.items()
                if value["Config"]["Labels"]["gca.scope"] == scope
            ).encode()
        if args[0] == "inspect":
            return json.dumps([self.containers[args[1]]]).encode()
        if args[0] == "rm":
            self.removed.append(args[-1])
            del self.containers[args[-1]]
            return b""
        raise AssertionError(args)


@pytest.fixture
def state(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    engine = Engine()
    monkeypatch.setattr(runner, "docker", engine)
    from app.coding import isolated_packaging
    # This fixture proves journal transitions only, not native Linux enforcement.
    monkeypatch.setattr(isolated_packaging, "require_native_cleanup", lambda container_id: None)
    return tmp_path, engine


def expire(journal, job):
    with journal.db:
        journal.db.execute("UPDATE jobs SET deadline=0 WHERE id=?", (job["id"],))
    job["deadline"] = 0


def test_live_owner_excludes_concurrent_launch_and_reaper(state):
    directory, engine = state
    with runner.Journal(directory):
        with pytest.raises(BlockingIOError):
            with runner.Journal(directory):
                pytest.fail("Second owner obtained the same slot")
    with runner.Journal(directory):
        pass
    assert engine.removed == []


def test_intent_survives_process_context_loss_and_blocks_new_work(state):
    directory, _ = state
    with runner.Journal(directory) as journal:
        job = journal.begin("python_compile", IMAGE, 20)
        scope = journal.scope
    with runner.Journal(directory) as journal:
        assert journal.scope == scope
        assert journal.db.execute("SELECT state FROM jobs WHERE id=?", (job["id"],)).fetchone()[0] == "creating"
        with pytest.raises(runner.IsolationError, match="unreconciled"):
            journal.begin("python_compile", IMAGE, 20)


def test_reaper_removes_only_expired_exact_identity_and_recovers_slot(state):
    directory, engine = state
    with runner.Journal(directory) as journal:
        job = journal.begin("python_compile", IMAGE, 20)
        engine.add(job, journal.scope)
        assert journal.reap()["cleaned_job_ids"] == []
        expire(journal, job)
        result = journal.reap()
        assert result["cleaned_job_ids"] == [job["id"]]
        assert engine.removed == [CID]
        assert journal.begin("python_compile", IMAGE, 20)["id"] != job["id"]


@pytest.mark.parametrize("mismatch", ["image", "scope", "job", "purpose"])
def test_identity_mismatch_never_deletes_other_container_or_releases_slot(state, mismatch):
    directory, engine = state
    with runner.Journal(directory) as journal:
        job = journal.begin("python_compile", IMAGE, 20)
        expire(journal, job)
        engine.add(job, journal.scope)
        if mismatch == "image":
            engine.containers[CID]["Image"] = "sha256:" + "c" * 64
        else:
            engine.containers[CID]["Config"]["Labels"]["gca." + mismatch] = "other"
        result = journal.reap()
        assert result["uncertain_job_ids"] == [job["id"]]
        assert engine.removed == []
        with pytest.raises(runner.IsolationError, match="unreconciled"):
            journal.begin("python_compile", IMAGE, 20)


def test_absence_before_deadline_is_not_proof_of_failed_creation(state):
    directory, _ = state
    with runner.Journal(directory) as journal:
        job = journal.begin("python_compile", IMAGE, 20)
        with pytest.raises(runner.IsolationError, match="uncertain"):
            journal.cleanup(job)
        expire(journal, job)
        assert journal.reap()["cleaned_job_ids"] == [job["id"]]


def test_replaced_container_id_is_not_deleted_even_with_copied_labels(state):
    directory, engine = state
    with runner.Journal(directory) as journal:
        job = journal.begin("python_compile", IMAGE, 20)
        journal.record(job, "running", "c" * 64)
        expire(journal, job)
        engine.add(job, journal.scope)
        assert journal.reap()["uncertain_job_ids"] == [job["id"]]
        assert engine.removed == []


def test_renamed_scoped_container_is_uncertain_not_mistaken_for_absence(state):
    directory, engine = state
    with runner.Journal(directory) as journal:
        job = journal.begin("python_compile", IMAGE, 20)
        expire(journal, job)
        engine.add(job, journal.scope, Name="/renamed")
        assert journal.reap()["uncertain_job_ids"] == [job["id"]]
        assert engine.removed == []
        with pytest.raises(runner.IsolationError, match="unreconciled"):
            journal.begin("python_compile", IMAGE, 20)


def test_late_container_after_observed_absence_is_still_reaped(state):
    directory, engine = state
    with runner.Journal(directory) as journal:
        job = journal.begin("python_compile", IMAGE, 20)
        expire(journal, job)
        journal.cleanup(job)
        engine.add(job, journal.scope)
        assert journal.reap()["cleaned_job_ids"] == [job["id"]]
        assert engine.removed == [CID]


def test_unknown_scoped_container_is_reported_not_deleted(state):
    directory, engine = state
    with runner.Journal(directory) as journal:
        engine.add({"id": "a" * 32, "name": "gca-validation-" + "a" * 32, "image": IMAGE},
                   journal.scope)
        assert journal.reap()["untracked_container_count"] == 1
        assert engine.removed == []


def test_provider_outage_does_not_mean_absence(state):
    directory, engine = state
    with runner.Journal(directory) as journal:
        job = journal.begin("python_compile", IMAGE, 20)
        expire(journal, job)
        engine.unavailable = True
        with pytest.raises(runner.IsolationError, match="unavailable"):
            journal.reap()
        assert journal.db.execute("SELECT state FROM jobs").fetchone()[0] == "creating"


def test_journal_never_contains_source_or_output_and_files_are_private(state):
    directory, _ = state
    with runner.Journal(directory) as journal:
        job = journal.begin("python_compile", IMAGE, 20)
        journal.record(job, "cleanup_uncertain")
        fields = [row[1] for row in journal.db.execute("PRAGMA table_info(jobs)")]
        assert not {"source", "request", "stdout", "stderr", "credentials"}.intersection(fields)
    assert Path(directory / "jobs.sqlite3").stat().st_mode & 0o077 == 0


def packaging_binding():
    return {"build_job_id": str(uuid4()), "request_hash": "d" * 64,
            "execution_deadline_unix": int(time.time()) + 120}


def test_legacy_journal_upgrade_preserves_live_slot(state):
    directory, _ = state
    db = sqlite3.connect(directory / "jobs.sqlite3")
    db.execute("CREATE TABLE jobs(id TEXT PRIMARY KEY,name TEXT NOT NULL UNIQUE,image TEXT NOT NULL,"
               "profile TEXT NOT NULL,deadline INTEGER NOT NULL,state TEXT NOT NULL,"
               "container_id TEXT,updated_at INTEGER NOT NULL)")
    db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?)",
               ("c" * 32, "gca-validation-" + "c" * 32, IMAGE, "python_compile", 123, "creating", None, 1))
    db.commit()
    db.close()
    with runner.Journal(directory) as journal:
        assert journal.db.execute("SELECT purpose FROM jobs").fetchone()[0] == "validation"
        with pytest.raises(runner.IsolationError, match="unreconciled"):
            journal.begin("image_packaging", IMAGE, 20, packaging_binding(), purpose="image_packaging")


def test_packaging_and_validation_share_crash_persistent_slot_and_claim_tombstone(state):
    directory, engine = state
    binding = packaging_binding()
    with runner.Journal(directory) as journal:
        job = journal.begin("image_packaging", IMAGE, 20, binding, purpose="image_packaging")
        assert job["name"] == "gca-packaging-" + job["id"]
    with runner.Journal(directory) as journal:
        with pytest.raises(runner.IsolationError, match="unreconciled"):
            journal.begin("python_compile", IMAGE, 20)
        engine.add(job, journal.scope)
        expire(journal, job)
        assert journal.reap()["cleaned_job_ids"] == [job["id"]]
        observation = journal.observe_claim(binding["build_job_id"])
        assert observation == {"claim_id": binding["build_job_id"], "packaging_job_id": job["id"],
                               "purpose": "image_packaging", "request_hash": binding["request_hash"],
                               "binding_hash": hashlib.sha256(json.dumps(
                                   binding, sort_keys=True, separators=(",", ":"),
                               ).encode()).hexdigest(), "state": "cleaned", "cleanup_verified": True,
                               "packaging_success": None}
        # Process absence says nothing about whether an image was built.
        assert observation["packaging_success"] is None
        with pytest.raises(runner.IsolationError, match="consumed"):
            journal.begin("python_compile", IMAGE, 20, {"claim_id": binding["build_job_id"],
                                                       "request_hash": "e" * 64,
                                                       "deadline": int(time.time()) + 100})
        assert journal.begin("python_compile", IMAGE, 20)["purpose"] == "validation"


@pytest.mark.parametrize("mismatch", ["image", "scope", "job", "purpose"])
def test_packaging_reaper_requires_exact_identity(state, mismatch):
    directory, engine = state
    with runner.Journal(directory) as journal:
        job = journal.begin("image_packaging", IMAGE, 20, packaging_binding(), purpose="image_packaging")
        expire(journal, job)
        engine.add(job, journal.scope)
        if mismatch == "image":
            engine.containers[CID]["Image"] = "sha256:" + "f" * 64
        else:
            engine.containers[CID]["Config"]["Labels"]["gca." + mismatch] = "validation"
        assert journal.reap()["uncertain_job_ids"] == [job["id"]]
        assert not engine.removed


def test_late_packaging_container_revokes_cleanup_observation_and_is_reaped(state):
    directory, engine = state
    binding = packaging_binding()
    with runner.Journal(directory) as journal:
        job = journal.begin("image_packaging", IMAGE, 20, binding, purpose="image_packaging")
        expire(journal, job)
        journal.cleanup(job, create_not_attempted=True)
        engine.add(job, journal.scope)
        assert journal.observe_claim(binding["build_job_id"])["cleanup_verified"] is False
        assert journal.reap()["cleaned_job_ids"] == [job["id"]]
        assert journal.observe_claim(binding["build_job_id"])["cleanup_verified"] is True


@pytest.mark.parametrize("binding,purpose,profile", [
    ({"claim_id": str(uuid4()), "build_job_id": str(uuid4())}, "validation", "python_compile"),
    ({"build_job_id": str(uuid4())}, "validation", "python_compile"),
    ({"claim_id": str(uuid4())}, "image_packaging", "image_packaging"),
    (None, "image_packaging", "image_packaging"),
    ({"build_job_id": "bad"}, "image_packaging", "image_packaging"),
    ({"build_job_id": str(uuid4())}, "image_packaging", "python_compile"),
])
def test_wrong_purpose_or_claim_rejected_before_intent(state, binding, purpose, profile):
    with runner.Journal(state[0]) as journal:
        with pytest.raises(runner.IsolationError):
            journal.begin(profile, IMAGE, 20, binding, purpose=purpose)
        assert journal.db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
