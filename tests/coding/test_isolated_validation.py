import io
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

import pytest

from app.coding import isolated_validation as runner


IMAGE = "sha256:" + "a" * 64
POLICY = {"version": 2, "runtime": "runsc", "images": {"python_compile": IMAGE}}


def test_trusted_readiness_hook_runs_before_output_collection_and_is_not_a_frame_argument(monkeypatch):
    monkeypatch.setattr(runner, 'DOCKER', sys.executable)
    observed = []
    result = runner.execute_bounded(['-I', '-c', 'print("synthetic-ready")'], 5,
                                    on_started=lambda child: observed.append(child.pid))
    assert len(observed) == 1 and result['success'] is True
    assert result['stdout'].strip() == 'synthetic-ready'


def test_readiness_hook_failure_terminates_only_its_owned_client_process(monkeypatch):
    monkeypatch.setattr(runner, 'DOCKER', sys.executable)
    observed = []

    def deny(child):
        observed.append(child)
        raise runner.IsolationError('synthetic native verification denied')

    with pytest.raises(runner.IsolationError, match='native verification denied'):
        runner.execute_bounded(['-I', '-c', 'import time; time.sleep(10)'], 5, on_started=deny)
    assert len(observed) == 1 and observed[0].poll() is not None


@pytest.fixture(autouse=True)
def journal_directory(tmp_path_factory, monkeypatch):
    directory = tmp_path_factory.mktemp("validation-journal")
    directory.chmod(0o700)
    monkeypatch.setitem(POLICY, "state_directory", str(directory))


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app/main.py").write_text("answer = 42\n")
    return tmp_path


def test_snapshot_excludes_credentials_and_resets_metadata(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app/main.py").write_text("answer = 42\n")
    (tmp_path / ".env").write_text("SECRET=not-for-workload")
    (tmp_path / ".env-private").mkdir()
    (tmp_path / ".env-private/secret").write_text("no")
    (tmp_path / ".gca-local").mkdir()
    (tmp_path / ".gca-local/key").write_text("no")
    with tarfile.open(fileobj=io.BytesIO(runner.source_archive(tmp_path))) as archive:
        assert archive.getnames() == ["app", "app/main.py"]
        assert all(member.uid == 65532 for member in archive.getmembers())
        assert archive.extractfile("app/main.py").read() == b"answer = 42\n"


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_snapshot_rejects_links_and_devices(tmp_path, kind):
    original = tmp_path / "source"
    original.write_text("secret")
    target = tmp_path / "linked"
    if kind == "symlink":
        target.symlink_to(original)
    elif kind == "hardlink":
        os.link(original, target)
    else:
        os.mkfifo(target)
    with pytest.raises(runner.IsolationError):
        runner.source_archive(tmp_path)


def test_snapshot_has_byte_and_entry_caps(tmp_path, monkeypatch):
    (tmp_path / "large").write_text("abc")
    monkeypatch.setattr(runner, "MAX_SOURCE_BYTES", 2)
    with pytest.raises(runner.IsolationError, match="byte limit"):
        runner.source_archive(tmp_path)
    monkeypatch.setattr(runner, "MAX_SOURCE_BYTES", 10)
    monkeypatch.setattr(runner, "MAX_SOURCE_ENTRIES", 0)
    with pytest.raises(runner.IsolationError, match="entry limit"):
        runner.source_archive(tmp_path)


def test_policy_requires_operator_path_and_immutable_images(tmp_path):
    # An operator-owned path under HOME, not world-writable /tmp.
    with tempfile.TemporaryDirectory(prefix=".gca-policy-test-", dir=Path.home()) as directory:
        config = Path(directory) / "policy.json"
        state = Path(directory) / "journal"
        state.mkdir(mode=0o700)
        policy = {**POLICY, "state_directory": str(state)}
        config.write_text(json.dumps(policy))
        config.chmod(0o600)
        assert runner.load_policy(config, tmp_path) == policy
        config.chmod(0o666)
        with pytest.raises(runner.IsolationError, match="untrusted identity"):
            runner.load_policy(config, tmp_path)
        config.chmod(0o600)
        config.write_text(json.dumps({**POLICY, "images": {"python_compile": "python:latest"}}))
        with pytest.raises(runner.IsolationError, match="immutable"):
            runner.load_policy(config, tmp_path)


def test_policy_inside_repository_is_not_authority(tmp_path):
    config = tmp_path / "policy.json"
    config.write_text(json.dumps(POLICY))
    with pytest.raises(runner.IsolationError, match="outside"):
        runner.load_policy(config, tmp_path)


@pytest.mark.parametrize("tampered", [False, True])
def test_missing_toolchain_reload_requires_operator_archive_digest(monkeypatch, tampered):
    with tempfile.TemporaryDirectory(prefix=".gca-offline-toolchain-", dir=Path.home()) as directory:
        root = Path(directory)
        body = b"synthetic archive bytes"
        archive = root / (IMAGE.removeprefix("sha256:") + ".tar")
        archive.write_bytes(body + (b"tampered" if tampered else b""))
        archive.chmod(0o600)
        registry = root / "toolchain-registry.json"
        registry.write_text(json.dumps({"version": 1, "images": {IMAGE: {
            "archive_path": str(archive), "archive_bytes": archive.stat().st_size,
            "archive_sha256": hashlib.sha256(body).hexdigest(),
            "repository_digest": "registry.example/toolchain@sha256:" + "a" * 64,
        }}}))
        registry.chmod(0o600)
        monkeypatch.setattr(runner, "TOOLCHAIN_REGISTRY", registry)
        calls = []

        def docker(*args, **kwargs):
            calls.append(args)
            if args[:2] == ("image", "inspect"):
                if len(calls) == 1:
                    raise runner.IsolationError("missing image")
                return IMAGE.encode()
            return b""

        monkeypatch.setattr(runner, "docker", docker)
        if tampered:
            with pytest.raises(runner.IsolationError, match="archive digest mismatch"):
                runner.ensure_toolchain_image(IMAGE)
            assert len(calls) == 1
        else:
            runner.ensure_toolchain_image(IMAGE)
            assert calls[1] == ("image", "load", "--input", str(archive))
            assert not any("pull" in call for call in calls)


def fake_transport(monkeypatch, *, fail=None, mounts=None):
    calls = []
    container = {}

    def call(*args, **kwargs):
        calls.append((args, kwargs))
        if args[0] == "create":
            container.update({
                "Name": "/" + args[args.index("--name") + 1], "Image": IMAGE,
                "Config": {"Labels": dict(
                    item.removeprefix("--label=").split("=", 1)
                    for item in args if item.startswith("--label=")
                )}, "Mounts": mounts or [],
            })
        if args[0] == fail:
            raise subprocess.TimeoutExpired(args, 30)
        if args[0] == "create":
            return b"c" * 64
        if args[:2] == ("image", "inspect"):
            return IMAGE.encode()
        if args[0] == "inspect":
            return json.dumps([container]).encode()
        if args[0] == "ps":
            return b"c" * 64 if container else b""
        if args[0] == "rm":
            container.clear()
        return b""

    monkeypatch.setattr(runner, "docker", call)
    monkeypatch.setattr(runner, "execute_bounded", lambda args, timeout: {
        "success": True, "exit_code": 0, "stdout": "", "stderr": "",
    })
    return calls


def test_validator_copies_snapshot_without_host_mount_or_credentials(tmp_path, monkeypatch):
    (tmp_path / "app").mkdir()
    (tmp_path / "app/main.py").write_text("answer=42")
    calls = fake_transport(monkeypatch)
    result = runner.validate(tmp_path, "python_compile", 10, POLICY)
    assert result["isolation"] == "gvisor"
    create = next(args for args, _ in calls if args[0] == "create")
    assert "--runtime=runsc" in create
    assert "--network=none" in create
    assert "--pull=never" in create
    assert "--ulimit=nofile=256:256" in create
    assert "--tmpfs=/workspace:rw,nosuid,nodev,exec,size=256m,mode=1777" in create
    assert "--tmpfs=/tmp:rw,nosuid,nodev,exec,size=64m,mode=1777" in create
    assert not any(item.startswith(("--mount", "--volume")) for item in create)
    assert {item for item in create if item.startswith("--env=")} == {
        "--env=HOME=/tmp", "--env=CI=true",
    }
    assert any(args[0] == "exec" and kwargs["payload"] for args, kwargs in calls)
    assert not any(args[0] == "cp" for args, _ in calls)
    assert calls[-1][0][:3] == ("rm", "--force", "--volumes")


@pytest.mark.parametrize('denied', [False, True])
def test_operator_fixture_enforcement_runs_before_source_extraction_and_cleanup_after_removal(
        workspace, monkeypatch, denied):
    calls = fake_transport(monkeypatch)
    observations = []

    def readiness(journal, job, cid):
        assert any(args[0] == 'start' for args, _ in calls)
        assert not any(args[0] == 'exec' for args, _ in calls)
        assert job['state'] == 'running' and cid == 'c' * 64
        observations.append('ready')
        if denied:
            raise runner.IsolationError('native enforcement denied')
        return {'local_mock_only': True}

    def cleanup(cid):
        assert calls[-1][0][:3] == ('rm', '--force', '--volumes')
        assert cid == 'c' * 64
        observations.append('native_cleanup')

    with runner.Journal(Path(POLICY['state_directory'])) as journal:
        job = journal.begin('python_compile', IMAGE, 10)
        values = (runner.source_archive(workspace), 'python_compile', 10, POLICY, journal, job)
        hooks = {'readiness': readiness, 'native_cleanup': cleanup, 'proof_cgroup': 'gcapackaging.slice'}
        if denied:
            with pytest.raises(runner.IsolationError, match='native enforcement denied'):
                runner._execute_archive(*values, **hooks)
            assert not any(args[0] == 'exec' for args, _ in calls)
        else:
            result = runner._execute_archive(*values, **hooks)
            assert result['native_enforcement'] == {'local_mock_only': True}
            assert result['executed_commands'] == [list(runner.PROFILES['python_compile'][1])]
        assert journal.db.execute('SELECT state FROM jobs').fetchone()[0] == 'cleaned'
    assert observations == ['ready', 'native_cleanup']
    create = next(args for args, _ in calls if args[0] == 'create')
    assert '--pids-limit=64' in create and '--cgroup-parent=gcapackaging.slice' in create
    assert '--ipc=private' in create and '--cgroupns=private' in create


@pytest.mark.parametrize('failure', [RuntimeError, OSError])
def test_native_fixture_cleanup_failure_overrides_success_and_retains_uncertainty(workspace, monkeypatch, failure):
    calls = fake_transport(monkeypatch)

    def deny(cid):
        assert calls[-1][0][:3] == ('rm', '--force', '--volumes')
        raise failure('Sentry still live')

    with runner.Journal(Path(POLICY['state_directory'])) as journal:
        job = journal.begin('python_compile', IMAGE, 10)
        with pytest.raises(runner.IsolationError, match='cleanup uncertain'):
            runner._execute_archive(runner.source_archive(workspace), 'python_compile', 10, POLICY, journal, job,
                                    readiness=lambda *args: {'local_mock_only': True},
                                    native_cleanup=deny, proof_cgroup='gcapackaging.slice')
        row = journal.db.execute('SELECT state,container_id FROM jobs').fetchone()
        assert row['state'] == 'cleanup_uncertain' and row['container_id'] == 'c' * 64
        with pytest.raises(runner.IsolationError, match='unreconciled'):
            journal.require_available()


@pytest.mark.parametrize("phase", ["create", "start", "inspect", "exec"])
def test_cleanup_after_control_failure(workspace, monkeypatch, phase):
    calls = fake_transport(monkeypatch, fail=phase)
    expected = runner.IsolationError if phase == "inspect" else subprocess.TimeoutExpired
    with pytest.raises(expected):
        runner.validate(workspace, "python_compile", 10, POLICY)
    assert calls[-1][0][0] == ("inspect" if phase == "inspect" else "rm")


def test_cleanup_uncertainty_overrides_validation_success(workspace, monkeypatch):
    fake_transport(monkeypatch, fail="rm")
    with pytest.raises(runner.IsolationError, match="cleanup uncertain"):
        runner.validate(workspace, "python_compile", 10, POLICY)


def test_unapproved_profile_never_creates_container(tmp_path, monkeypatch):
    calls = fake_transport(monkeypatch)
    with pytest.raises(runner.IsolationError, match="No approved"):
        runner.validate(tmp_path, "web_build", 10, POLICY)
    assert not calls


def test_implicit_volume_stops_before_start(workspace, monkeypatch):
    calls = fake_transport(monkeypatch, mounts=[{"Type": "volume"}])
    with pytest.raises(runner.IsolationError, match="persistent mount"):
        runner.validate(workspace, "python_compile", 10, POLICY)
    assert not any(args[0] == "start" for args, _ in calls)


def test_empty_python_compile_cannot_claim_success(tmp_path, monkeypatch):
    calls = fake_transport(monkeypatch)
    with pytest.raises(runner.IsolationError, match="No Python source"):
        runner.validate(tmp_path, "python_compile", 10, POLICY)
    assert calls == []


def test_generated_output_is_bounded_but_exit_status_preserved(monkeypatch):
    monkeypatch.setattr(runner, "DOCKER", sys.executable)
    result = runner.execute_bounded(["-c", "import sys; print('x'*300000); sys.exit(3)"], 3)
    assert len(result["stdout"].encode()) == runner.MAX_OUTPUT_BYTES
    assert result["output_truncated"] is True
    assert result["success"] is False
    assert result["exit_code"] == 3


def test_generated_process_deadline_is_enforced(monkeypatch):
    monkeypatch.setattr(runner, "DOCKER", sys.executable)
    with pytest.raises(runner.IsolationError, match="deadline"):
        runner.execute_bounded(["-c", "import time; time.sleep(30)"], 1)


def generated_stack(root: Path):
    for path, content in {
        "frontend/package.json": '{"name":"generated-app","version":"1.0.0"}',
        "frontend/package-lock.json": '{"name":"generated-app","lockfileVersion":3}',
        "frontend/index.html": "<div id='root'></div>",
        "frontend/src/main.tsx": "export const App = () => null;",
        "frontend/src/App.test.tsx": "test('renders', () => {});",
        "backend/pom.xml": "<project></project>",
        "backend/src/main/java/example/App.java": "class App {}",
        "backend/src/test/java/example/AppTest.java": "class AppTest {}",
    }.items():
        file = root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content)


def surefire_archive(*, tests=1, skipped=0, failures=0, errors=0, xml_override=None):
    xml = (xml_override or f'<testsuite tests="{tests}" skipped="{skipped}" '
           f'failures="{failures}" errors="{errors}"></testsuite>').encode()
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        item = tarfile.TarInfo("target/surefire-reports/TEST-example.AppTest.xml")
        item.size = len(xml)
        archive.addfile(item, io.BytesIO(xml))
    return output.getvalue()


@pytest.mark.parametrize("values", [
    {"tests": 0}, {"tests": 2, "skipped": 2}, {"tests": 1, "failures": 1},
    {"tests": 1, "errors": 1}, {"xml_override": "<!DOCTYPE x [<!ENTITY a 'x'>]><testsuite/>"},
    {"xml_override": "<testsuite tests='one' skipped='0' failures='0' errors='0'/>"},
])
def test_surefire_report_rejects_nonexecuted_or_invalid_evidence(values):
    assert runner.surefire_report_passed(surefire_archive(**values)) is False


def test_surefire_report_accepts_executed_passing_tests():
    assert runner.surefire_report_passed(surefire_archive(tests=3, skipped=1)) is True


@pytest.mark.parametrize("report", [
    surefire_archive(tests=0), surefire_archive(tests=2, skipped=2), b"not a tar archive",
])
def test_spring_command_exit_zero_cannot_mask_empty_test_evidence(
        tmp_path, monkeypatch, report):
    generated_stack(tmp_path)
    fake_transport(monkeypatch)

    def execute(args, _timeout, **kwargs):
        return {"success": True, "exit_code": 0, "output_truncated": False,
                "stdout": report if kwargs.get("binary") else "", "stderr": ""}

    monkeypatch.setattr(runner, "execute_bounded", execute)
    result = runner.validate(tmp_path, "spring_test", 10,
                             {**POLICY, "images": {"spring_test": IMAGE}})
    assert result["success"] is False
    assert result["stderr"] == "No executed passing Surefire test report"


@pytest.mark.parametrize("profile,missing", [
    ("react_build", "frontend/package-lock.json"),
    ("react_test", "frontend/src/App.test.tsx"),
    ("spring_test", "backend/src/test/java/example/AppTest.java"),
    ("spring_package", "backend/pom.xml"),
])
def test_generated_profile_denies_missing_source_before_docker(tmp_path, monkeypatch,
                                                               profile, missing):
    generated_stack(tmp_path)
    (tmp_path / missing).unlink()
    calls = fake_transport(monkeypatch)
    policy = {**POLICY, "images": {profile: IMAGE}}
    with pytest.raises(runner.IsolationError, match="required|test source"):
        runner.validate(tmp_path, profile, 10, policy)
    assert calls == []


def test_generated_build_artifacts_cannot_be_supplied_as_source(tmp_path):
    generated_stack(tmp_path)
    (tmp_path / "frontend/dist").mkdir()
    (tmp_path / "frontend/dist/index.html").write_text("stale build")
    (tmp_path / "backend/target").mkdir()
    (tmp_path / "backend/target/stale.jar").write_text("stale jar")
    with tarfile.open(fileobj=io.BytesIO(runner.source_archive(tmp_path))) as archive:
        names = archive.getnames()
    assert not any("/dist" in name or "/target" in name for name in names)


@pytest.mark.parametrize("profile,expected", [
    ("react_test", ["npm", "node_modules/.bin/vitest"]),
    ("react_build", ["npm", "node_modules/.bin/vite", "test"]),
    ("spring_test", ["mvn", "tar"]),
    ("spring_package", ["mvn", "tar", "find"]),
])
def test_generated_profiles_run_only_fixed_offline_commands(tmp_path, monkeypatch,
                                                            profile, expected):
    generated_stack(tmp_path)
    calls = fake_transport(monkeypatch)
    commands = []

    def execute(args, timeout, **kwargs):
        assert 0 < timeout <= 10
        commands.append(args[4:])
        return {"success": True, "exit_code": 0,
                "stdout": (surefire_archive() if kwargs.get("binary") else
                           "target/app.jar\n" if args[4] == "find" else ""),
                "stderr": "", "output_truncated": False}

    monkeypatch.setattr(runner, "execute_bounded", execute)
    policy = {**POLICY, "images": {profile: IMAGE}}
    result = runner.validate(tmp_path, profile, 10, policy)
    assert result["success"] is True
    assert [command[0] for command in commands] == expected
    assert any("--network=none" in args for args, _ in calls if args[0] == "create")
    limit = 1024 if profile.startswith('spring_') else 256
    assert any(f'--ulimit=nofile={limit}:{limit}' in args for args, _ in calls if args[0] == 'create')
    assert calls[-1][0][:3] == ("rm", "--force", "--volumes")


@pytest.mark.parametrize("profile", ["react_build", "spring_test", "spring_package"])
def test_missing_generated_artifact_is_a_failed_validation(tmp_path, monkeypatch, profile):
    generated_stack(tmp_path)
    fake_transport(monkeypatch)
    commands = []

    def execute(args, _timeout, **kwargs):
        commands.append(args[4])
        found = args[4] != "test"
        return {"success": found, "exit_code": 0 if found else 1,
                "stdout": b"" if kwargs.get("binary") else "", "stderr": "",
                "output_truncated": False}

    monkeypatch.setattr(runner, "execute_bounded", execute)
    result = runner.validate(tmp_path, profile, 10,
                             {**POLICY, "images": {profile: IMAGE}})
    assert result["success"] is False and result["exit_code"] == 1
    assert commands[-1] in {"test", "find", "tar"}


def test_failed_offline_install_never_runs_generated_react_code(tmp_path, monkeypatch):
    generated_stack(tmp_path)
    calls = fake_transport(monkeypatch)
    commands = []

    def execute(args, _timeout):
        commands.append(args[4])
        return {"success": False, "exit_code": 1, "stdout": "",
                "stderr": "offline dependency unavailable", "output_truncated": False}

    monkeypatch.setattr(runner, "execute_bounded", execute)
    result = runner.validate(tmp_path, "react_build", 10,
                             {**POLICY, "images": {"react_build": IMAGE}})
    assert result["success"] is False
    assert commands == ["npm"]
    assert calls[-1][0][:3] == ("rm", "--force", "--volumes")
