from dataclasses import replace
import gzip
import hashlib
import io
import json
import tarfile
import time
import zipfile
from uuid import uuid4

import pytest

from app.coding.build_artifact import inspect_build_artifact
from app.coding import oci_packager as packager
from tests.coding.test_build_artifact import boot_jar


def archive(files):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as tar:
        for name, value in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(value)
            tar.addfile(member, io.BytesIO(value))
    return output.getvalue()


def base(role="nginx_static_v1", **config_overrides):
    uncompressed = archive({"synthetic-runtime": b"not a live executable runtime"})
    layer = gzip.compress(uncompressed, mtime=0)
    config = packager.canonical({
        "architecture": "amd64", "os": "linux",
        "config": {"Env": ["PATH=/usr/bin"], "Cmd": ["old-command"],
                   "Labels": {"approved": "synthetic"}},
        "rootfs": {"type": "layers", "diff_ids": [packager.digest(uncompressed)]},
        "history": [], **config_overrides,
    })
    manifest = packager.canonical({
        "schemaVersion": 2, "mediaType": packager.OCI_MANIFEST,
        "config": {"digest": packager.digest(config), "size": len(config),
                   "mediaType": packager.OCI_CONFIG},
        "layers": [{"digest": packager.digest(layer), "size": len(layer),
                    "mediaType": packager.OCI_LAYER}],
    })
    return packager.ApprovedRuntimeBase(role, packager.digest(manifest), manifest, config, (layer,))


def case(profile="react_build", files=None):
    files = files or ({"dist/index.html": b"<html>Built app</html>",
                       "dist/assets/index.js": b"console.log('built')"}
                      if profile == "react_build" else {"target/app.jar": boot_jar()})
    payload = archive(files)
    manifest = inspect_build_artifact(payload, profile)
    job = "a" * 32
    claim = str(uuid4())
    source = "b" * 64
    reference = {
        "artifact_id": job + "-" + manifest.archive_sha256 + ".tar",
        "archive_sha256": manifest.archive_sha256,
        "file_set_sha256": hashlib.sha256(json.dumps(
            manifest.file_hashes, separators=(",", ":")).encode()).hexdigest(),
        "file_count": manifest.file_count, "content_bytes": manifest.content_bytes,
        "profile": profile, "validation_job_id": job, "claim_id": claim,
        "source_hash": source, "expires_at_unix": int(time.time()) + 3600,
    }
    return payload, reference, dict(profile=profile, source_hash=source, claim_id=claim, job_id=job)


@pytest.mark.parametrize("profile,role,entrypoint", [
    ("react_build", "nginx_static_v1", "/usr/sbin/nginx"),
    ("spring_package", "temurin_java17_v1", "/opt/java/openjdk/bin/java"),
])
def test_packages_exact_outputs_without_running_generated_code(profile, role, entrypoint):
    payload, reference, arguments = case(profile)
    runtime = base(role)
    output = packager.package_validated_artifact(runtime, payload, reference, **arguments)
    repeated = packager.package_validated_artifact(runtime, payload, reference, **arguments)
    assert output == repeated
    assert output.evidence_hash == repeated.evidence_hash
    assert output.evidence["generated_code_executed"] is False
    config = json.loads(output.config)
    assert config["config"]["User"] == "65532:65532"
    assert config["config"]["Entrypoint"] == [entrypoint]
    assert config["config"]["OnBuild"] == []
    assert config["rootfs"]["diff_ids"][-1] == packager.digest(gzip.decompress(output.layer))
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(output.layer))) as tar:
        assert all(item.uid == 65532 and item.mode == 0o644 for item in tar.getmembers())
        if profile == "react_build":
            assert tar.extractfile("usr/share/nginx/html/index.html").read() == b"<html>Built app</html>"
            assert tar.extractfile("etc/nginx/nginx.conf").read() == packager.NGINX_CONFIG
        else:
            assert tar.getnames() == ["app/app.jar"]
            with tarfile.open(fileobj=io.BytesIO(payload)) as source_tar:
                assert tar.extractfile("app/app.jar").read() == source_tar.extractfile("target/app.jar").read()
    assert runtime.inspect()[1]["config"]["Cmd"] == ["old-command"]


def test_layout_contains_actual_content_addressed_blobs():
    payload, reference, arguments = case()
    output = packager.package_validated_artifact(base(), payload, reference, **arguments)
    with tarfile.open(fileobj=io.BytesIO(output.layout_archive())) as tar:
        assert json.load(tar.extractfile("oci-layout")) == {"imageLayoutVersion": "1.0.0"}
        descriptor = json.load(tar.extractfile("index.json"))["manifests"][0]
        assert descriptor["digest"] == output.manifest_digest
        for item in tar.getmembers():
            if item.name.startswith("blobs/sha256/"):
                assert hashlib.sha256(tar.extractfile(item).read()).hexdigest() == item.name.split('/')[-1]


def test_bounded_oci_annotations_are_preserved_in_base_identity_not_execution_authority():
    runtime = base()
    manifest = json.loads(runtime.manifest)
    manifest['annotations'] = {'org.opencontainers.image.version': 'public-example', 'org.gca.approved': 'true'}
    manifest['config']['annotations'] = {'example.metadata': 'not a command'}
    body = packager.canonical(manifest)
    annotated = replace(runtime, manifest=body, approved_manifest_digest=packager.digest(body))
    assert annotated.inspect()[0] == manifest
    payload, reference, arguments = case()
    output = packager.package_validated_artifact(annotated, payload, reference, **arguments)
    assert output.base_manifest_digest == packager.digest(body)
    assert json.loads(output.config)['config']['Entrypoint'] == ['/usr/sbin/nginx']
    assert output.evidence['generated_code_executed'] is False


@pytest.mark.parametrize('annotations', [None, [], {'key': 1}, {'x' * 257: 'v'},
                                        {'key': 'x' * 2049}, {str(i): 'x' for i in range(65)},
                                        {str(i): 'x' * 2048 for i in range(17)}])
def test_oci_annotations_remain_bounded_string_metadata(annotations):
    runtime = base();manifest = json.loads(runtime.manifest);manifest['annotations'] = annotations
    body = packager.canonical(manifest)
    with pytest.raises(packager.ImagePackagingError, match='annotations'):
        replace(runtime, manifest=body, approved_manifest_digest=packager.digest(body)).inspect()


def test_external_descriptor_urls_remain_rejected_even_with_annotations():
    runtime = base();manifest = json.loads(runtime.manifest)
    manifest['layers'][0]['annotations'] = {'example': 'metadata'}
    manifest['layers'][0]['urls'] = ['https://unapproved.example/blob']
    body = packager.canonical(manifest)
    with pytest.raises(packager.ImagePackagingError, match='pinned descriptor'):
        replace(runtime, manifest=body, approved_manifest_digest=packager.digest(body)).inspect()


@pytest.mark.parametrize("field,value", [
    ("archive_sha256", "f" * 64), ("file_set_sha256", "f" * 64),
    ("file_count", 100), ("content_bytes", 99999),
])
def test_changed_artifact_evidence_cannot_be_packaged(field, value):
    payload, reference, arguments = case()
    reference[field] = value
    with pytest.raises(ValueError):
        packager.package_validated_artifact(base(), payload, reference, **arguments)


@pytest.mark.parametrize("change", ["manifest", "config", "layer", "approval"])
def test_changed_base_blob_or_operator_pin_is_rejected(change):
    runtime = base()
    changes = {
        "manifest": {"manifest": runtime.manifest + b" "},
        "config": {"config": runtime.config + b" "},
        "layer": {"layers": (runtime.layers[0] + b"tampered",)},
        "approval": {"approved_manifest_digest": "sha256:" + "f" * 64},
    }
    with pytest.raises(packager.ImagePackagingError):
        replace(runtime, **changes[change]).inspect()


@pytest.mark.parametrize("changes", [
    {"architecture": "arm64"}, {"os": "windows"},
    {"config": {"OnBuild": ["RUN arbitrary"]}},
    {"config": {"Env": False}},
    {"rootfs": {"type": "layers", "diff_ids": ["sha256:" + "f" * 64]}},
    {"rootfs": {"type": "layers", "diff_ids": []}},
])
def test_operator_base_still_requires_safe_platform_and_coherent_rootfs(changes):
    with pytest.raises(packager.ImagePackagingError):
        base(**changes).inspect()


def test_bomb_layer_cannot_exceed_unpacked_budget(monkeypatch):
    monkeypatch.setattr(packager, "MAX_BASE_UNPACKED_BYTES", 10)
    with pytest.raises(packager.ImagePackagingError, match="unpacked"):
        base().inspect()


def test_layer_whiteouts_in_generated_content_are_rejected():
    payload, reference, arguments = case(files={
        "dist/index.html": b"ok", "dist/.wh.index.html": b"malicious deletion marker",
    })
    with pytest.raises(packager.ImagePackagingError, match="whiteouts"):
        packager.package_validated_artifact(base(), payload, reference, **arguments)


def test_runtime_role_is_not_interchangeable():
    payload, reference, arguments = case()
    with pytest.raises(packager.ImagePackagingError, match="role"):
        packager.package_validated_artifact(base("temurin_java17_v1"), payload, reference, **arguments)


def test_duplicate_json_keys_fail_even_in_a_pinned_manifest():
    runtime = base()
    manifest = runtime.manifest.replace(b'"schemaVersion":2', b'"schemaVersion":2,"schemaVersion":2')
    runtime = replace(runtime, manifest=manifest, approved_manifest_digest=packager.digest(manifest))
    with pytest.raises(packager.ImagePackagingError, match="metadata"):
        runtime.inspect()


def test_java21_class_cannot_be_packaged_as_java17_runtime():
    result = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(boot_jar())) as source, zipfile.ZipFile(result, "w") as target:
        for item in source.infolist():
            data = source.read(item)
            if item.filename.endswith(".class"):
                data = data[:6] + b"\x00\x41" + data[8:]
            target.writestr(item, data)
    payload, reference, arguments = case("spring_package", files={"target/app.jar": result.getvalue()})
    with pytest.raises(packager.ImagePackagingError, match="Java 17"):
        packager.package_validated_artifact(base("temurin_java17_v1"), payload, reference, **arguments)
