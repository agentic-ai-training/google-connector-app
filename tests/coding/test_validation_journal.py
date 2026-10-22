import json
import hashlib
import sqlite3
import time
from pathlib import Path
from uuid import uuid4

import pytest

from app.coding import isolated_validation as runner


@pytest.fixture(autouse=True)
def historical_fixed_scan_window(request, monkeypatch):
    # Only fixed-term scan scenarios use this clock. Other journal/process tests
    # keep their real clocks; no runtime deadline or production STOP is changed.
    name = request.node.name
    if name.startswith(('test_scan_', 'test_assessment_')) or name == 'test_expired_unknown_scan_creation_keeps_slot_uncertain':
        monkeypatch.setattr(time, 'time', lambda: 1791889200.0)


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


def scan_binding(*, scan_id=None, stage='inventory', inventory=None):
    return {'version': 1, 'scan_id': scan_id or str(uuid4()), 'role': 'nginx_static_v1',
            'stage': stage,
            'image_id': 'sha256:6b1f85a08c199d29d5b6d71ab9c27bd5b3b393492e01216a15758ff69c4be8b8',
            'manifest_sha256': runner.RUNTIME_SCAN_ROLES['nginx_static_v1'],
            'scanner_sha256': '93f9da8e4ba5e0c1c76d8234ed2494cf9afb0a96fd21953e424bb795f3299b8e',
            'signature_receipt_sha256': '712dc1f6a1d34e65d2c54390ba25da4942220e6bdbe9bf5ea8786b9927cfa112',
            'database_catalog_sha256': '79734a08f6dccc4623e6ef7d7bc523a05d4071d57370bde7a5f2cfd4c5086fd1',
            'inventory_sha256': inventory, 'controller_sha256': 'c' * 64,
            'stop_unix': 1792430672, 'deadline': int(time.time()) + 180}


def test_scan_intent_survives_crash_and_excludes_validation_and_packaging(state):
    directory, _ = state
    binding = scan_binding()
    with runner.Journal(directory) as journal:
        job = journal.begin_runtime_scan(binding)
        assert job['purpose'] == 'validation' and job['profile'] == 'runtime_inventory'
    with runner.Journal(directory) as journal:
        assert journal.observe_runtime_scan(binding['scan_id'], binding['role'], 'inventory')['state'] == 'creating'
        with pytest.raises(runner.IsolationError, match='unreconciled'):
            journal.begin('python_compile', IMAGE, 20)
        with pytest.raises(runner.IsolationError, match='unreconciled'):
            journal.begin('image_packaging', IMAGE, 20, packaging_binding(), purpose='image_packaging')


@pytest.mark.parametrize('fault', [
    {'stage': 'shell'}, {'role': 'postgres_private_v1'}, {'image_id': IMAGE},
    {'scanner_sha256': 'a' * 64}, {'signature_receipt_sha256': 'b' * 64},
    {'database_catalog_sha256': 'c' * 64}, {'manifest_sha256': 'd' * 64},
    {'deadline': True}, {'stop_unix': 1792430673}, {'scan_id': '../other'},
    {'inventory_sha256': 'e' * 64}, {'extra_command': 'bash'},
])
def test_scan_unapproved_fields_cannot_reserve_slot(state, fault):
    directory, _ = state
    with runner.Journal(directory) as journal:
        with pytest.raises(runner.IsolationError):
            journal.begin_runtime_scan({**scan_binding(), **fault})
        assert not journal.db.execute('SELECT 1 FROM jobs').fetchone()


def test_scan_deadline_bound_is_not_a_host_term_extension(state):
    directory, _ = state
    with runner.Journal(directory) as journal:
        for deadline in (int(time.time()) - 1, int(time.time()) + 300, 1792430673):
            with pytest.raises(runner.IsolationError, match='fixed operator'):
                journal.begin_runtime_scan({**scan_binding(), 'deadline': deadline})


def test_scan_profiles_are_not_normal_model_selected_commands(state):
    directory, _ = state
    assert not runner.RUNTIME_SCAN_PROFILES & runner.PROFILES.keys()
    with runner.Journal(directory) as journal:
        for profile in runner.RUNTIME_SCAN_PROFILES:
            with pytest.raises(runner.IsolationError, match='purpose/profile'):
                journal.begin(profile, IMAGE, 20)


def test_scan_tombstone_blocks_replay_even_with_changed_deadline_and_controller(state):
    directory, _ = state
    binding = scan_binding()
    with runner.Journal(directory) as journal:
        job = journal.begin_runtime_scan(binding)
        journal.cleanup(job, create_not_attempted=True)
        changed = {**binding, 'deadline': binding['deadline'] + 1, 'controller_sha256': 'd' * 64}
        with pytest.raises(runner.IsolationError, match='already consumed'):
            journal.begin_runtime_scan(changed)
        observed = journal.observe_runtime_scan(binding['scan_id'], binding['role'], 'inventory')
        assert observed['cleanup_verified'] and observed['scan_completed'] is None


def test_assessment_requires_exact_retained_inventory_not_just_a_hash(state):
    directory, _ = state
    binding = scan_binding(stage='assessment', inventory='e' * 64)
    with runner.Journal(directory) as journal:
        with pytest.raises(runner.IsolationError, match='exact retained inventory'):
            journal.begin_runtime_scan(binding)
        inventory = {**binding, 'stage': 'inventory', 'inventory_sha256': None}
        job = journal.begin_runtime_scan(inventory)
        journal.cleanup(job, create_not_attempted=True)
        key = journal.runtime_scan_key(binding['scan_id'], binding['role'], 'inventory')
        with journal.db:
            # Synthetic controller metadata only; this is not actual SBOM or native proof.
            journal.db.execute('INSERT INTO runtime_scan_outputs VALUES(?,?,?,?)', (key, 'f' * 64, 123, 'a' * 64))
        with pytest.raises(runner.IsolationError, match='exact retained inventory'):
            journal.begin_runtime_scan(binding)
        with journal.db:
            journal.db.execute('UPDATE runtime_scan_outputs SET output_sha256=? WHERE scan_key=?', ('e' * 64, key))
        job = journal.begin_runtime_scan(binding)
        assert job['profile'] == 'runtime_assessment'


def test_scan_reaper_requires_native_termination_after_removing_docker(state, monkeypatch):
    directory, engine = state
    from app.coding import isolated_packaging
    def native_uncertain(cid):
        assert cid == CID
        raise RuntimeError('synthetic helper still alive')
    monkeypatch.setattr(isolated_packaging, 'require_native_cleanup', native_uncertain)
    with runner.Journal(directory) as journal:
        job = journal.begin_runtime_scan(scan_binding())
        engine.add(job, journal.scope)
        expire(journal, job)
        result = journal.reap()
        assert result['uncertain_job_ids'] == [job['id']] and engine.removed == [CID]
        row = journal.db.execute('SELECT * FROM jobs WHERE id=?', (job['id'],)).fetchone()
        assert row['state'] == 'cleanup_uncertain' and row['container_id'] == CID
        with pytest.raises(runner.IsolationError, match='unreconciled'):
            journal.begin('python_compile', IMAGE, 20)
        monkeypatch.setattr(isolated_packaging, 'require_native_cleanup', lambda cid: None)
        assert journal.reap()['cleaned_job_ids'] == [job['id']]


def test_expired_unknown_scan_creation_keeps_slot_uncertain(state):
    directory, _ = state
    with runner.Journal(directory) as journal:
        job = journal.begin_runtime_scan(scan_binding())
        expire(journal, job)
        result = journal.reap()
        assert result['uncertain_job_ids'] == [job['id']] and result['cleaned_job_ids'] == []


def test_scan_observation_rechecks_late_native_helpers(state, monkeypatch):
    directory, engine = state
    from app.coding import isolated_packaging
    binding = scan_binding()
    with runner.Journal(directory) as journal:
        job = journal.begin_runtime_scan(binding)
        engine.add(job, journal.scope)
        journal.cleanup(job, create_confirmed=True)
        def late(cid):
            raise RuntimeError('synthetic late helper')
        monkeypatch.setattr(isolated_packaging, 'require_native_cleanup', late)
        result = journal.observe_runtime_scan(binding['scan_id'], binding['role'], 'inventory')
        assert result['state'] == 'cleanup_uncertain' and not result['cleanup_verified']
        assert result['scan_completed'] is None


def test_scan_binding_corruption_is_not_success_or_a_new_dispatch(state):
    directory, _ = state
    binding = scan_binding()
    with runner.Journal(directory) as journal:
        journal.begin_runtime_scan(binding)
        with journal.db:
            journal.db.execute("UPDATE runtime_scan_jobs SET binding='{}'")
        with pytest.raises(runner.IsolationError, match='fingerprint'):
            journal.observe_runtime_scan(binding['scan_id'], binding['role'], 'inventory')


def test_scan_history_is_bounded_before_creating_another_job(state):
    directory, _ = state
    with runner.Journal(directory) as journal:
        with journal.db:
            journal.db.executemany('INSERT INTO runtime_scan_jobs VALUES(?,?,?,?)',
                                  [(str(i), '{}', 'a' * 64, 'old-' + str(i)) for i in range(32)])
        with pytest.raises(runner.IsolationError, match='history capacity'):
            journal.begin_runtime_scan(scan_binding())
        assert journal.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 0


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
