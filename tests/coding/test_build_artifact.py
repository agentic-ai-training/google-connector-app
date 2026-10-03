import hashlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile

import pytest

from app.coding.build_artifact import (
    ARTIFACT_TTL_SECONDS, BuildArtifactError, inspect_build_artifact,
    load_build_artifact, purge_expired_build_artifacts, save_build_artifact,
)


def archive(files, *, extra_member=None):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as bundle:
        for name, body in files.items():
            item = tarfile.TarInfo(name)
            item.size = len(body)
            bundle.addfile(item, io.BytesIO(body))
        if extra_member is not None:
            bundle.addfile(extra_member)
    return output.getvalue()


def boot_jar(*, executable=True):
    output = io.BytesIO()
    with zipfile.ZipFile(output, mode="w") as jar:
        manifest = ("Manifest-Version: 1.0\n"
                    "Main-Class: org.springframework.boot.loader.launch.JarLauncher\n"
                    "Start-Class: example.App\n") if executable else "Manifest-Version: 1.0\n"
        jar.writestr("META-INF/MANIFEST.MF", manifest)
        jar.writestr("BOOT-INF/classes/example/App.class", b"class data")
        jar.writestr("BOOT-INF/lib/dependency.jar", b"dependency")
    return output.getvalue()


def test_react_artifact_has_bounded_hashed_files():
    payload = archive({"dist/index.html": b"<html>app</html>",
                       "dist/assets/main.js": b"app();"})
    result = inspect_build_artifact(payload, "react_build")
    assert result.archive_sha256 == hashlib.sha256(payload).hexdigest()
    assert result.file_hashes == tuple(sorted((
        ("dist/index.html", hashlib.sha256(b"<html>app</html>").hexdigest()),
        ("dist/assets/main.js", hashlib.sha256(b"app();").hexdigest()),
    )))
    assert result.file_count == 2


def test_spring_artifact_requires_one_executable_boot_jar():
    payload = archive({"target/app.jar": boot_jar()})
    result = inspect_build_artifact(payload, "spring_package")
    assert result.file_count == 1
    assert result.content_bytes == len(boot_jar())


@pytest.mark.parametrize("files,profile", [
    ({"dist/assets/main.js": b"app();"}, "react_build"),
    ({"dist/index.html": b""}, "react_build"),
    ({"dist/index.html": b"ok", "dist/.env": b"secret"}, "react_build"),
    ({"../dist/index.html": b"x"}, "react_build"),
    ({"dist/../secret": b"x"}, "react_build"),
    ({"target/app.jar": b"not-a-jar"}, "spring_package"),
    ({"target/app.jar": boot_jar(executable=False)}, "spring_package"),
    ({"target/app.jar": boot_jar(), "target/app-2.jar": boot_jar()}, "spring_package"),
    ({"target/app.jar": boot_jar(), "target/secret.txt": b"secret"}, "spring_package"),
])
def test_rejects_invalid_generated_artifacts(files, profile):
    with pytest.raises(BuildArtifactError):
        inspect_build_artifact(archive(files), profile)


def test_rejects_link_and_unsupported_profile():
    link = tarfile.TarInfo("dist/linked")
    link.type = tarfile.SYMTYPE
    link.linkname = "/etc/passwd"
    with pytest.raises(BuildArtifactError):
        inspect_build_artifact(archive({"dist/index.html": b"ok"}, extra_member=link),
                               "react_build")
    with pytest.raises(BuildArtifactError):
        inspect_build_artifact(archive({"dist/index.html": b"ok"}), "python_compile")


def test_owner_only_cas_rehashes_and_expires_artifact(tmp_path):
    payload = archive({"dist/index.html": b"<html>app</html>"})
    manifest = inspect_build_artifact(payload, "react_build")
    artifact_id = save_build_artifact(tmp_path, "a" * 32, payload, manifest)
    saved = tmp_path / "build-artifacts" / artifact_id
    assert saved.stat().st_mode & 0o077 == 0
    assert load_build_artifact(tmp_path, artifact_id, manifest.archive_sha256) == payload
    with pytest.raises(FileExistsError):
        save_build_artifact(tmp_path, "a" * 32, payload, manifest)
    saved.write_bytes(b"tampered")
    with pytest.raises(BuildArtifactError, match="content changed"):
        load_build_artifact(tmp_path, artifact_id, manifest.archive_sha256)
    old = time.time() - ARTIFACT_TTL_SECONDS - 2
    os.utime(saved, (old, old))
    assert purge_expired_build_artifacts(tmp_path) == 1
    assert not saved.exists()


def test_cas_rejects_public_directory_and_unsafe_reference(tmp_path):
    payload = archive({"dist/index.html": b"ok"})
    manifest = inspect_build_artifact(payload, "react_build")
    artifacts = tmp_path / "build-artifacts"
    artifacts.mkdir(mode=0o700)
    artifacts.chmod(0o755)
    with pytest.raises(BuildArtifactError, match="operator private"):
        save_build_artifact(tmp_path, "b" * 32, payload, manifest)
    artifacts.chmod(0o700)
    with pytest.raises(BuildArtifactError, match="Invalid build artifact reference"):
        load_build_artifact(tmp_path, "../outside", manifest.archive_sha256)


def test_launcher_bundle_loads_helper_with_python_isolated_mode():
    root = Path(__file__).resolve().parents[2] / "app/coding"
    with tempfile.TemporaryDirectory(prefix=".gca-launcher-bundle-", dir=Path.home()) as directory:
        installed = Path(directory)
        for name in ("isolated_validation.py", "build_artifact.py"):
            shutil.copyfile(root / name, installed / name)
            (installed / name).chmod(0o644)
        result = subprocess.run(
            [sys.executable, "-I", "-c", "import runpy,sys; "
             "m=runpy.run_path(sys.argv[1]); print(m['_artifact_helper']().MAX_STORED_ARTIFACTS)",
             str(installed / "isolated_validation.py")],
            capture_output=True, text=True, timeout=10, check=True,
        )
        assert result.stdout.strip() == "8"
