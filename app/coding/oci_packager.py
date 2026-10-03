"""Keyless, non-executing OCI assembly for verified React/Spring build outputs.

Internal trusted-controller code, not a model tool or deployment authorization.
An operator pins the complete runtime manifest; the assembler verifies its blobs,
copies only validated artifacts into one deterministic layer, and measures the new
manifest. It never runs a Dockerfile, generated code, shell, or network operation.
"""
from __future__ import annotations

from dataclasses import dataclass
import gzip
import hashlib
import io
import json
import re
import tarfile
import zipfile
from typing import Literal

from app.coding.build_artifact import (
    inspect_build_artifact, verify_artifact_reference,
)


MAX_BASE_BYTES = 512 * 1024 * 1024
MAX_METADATA_BYTES = 1024 * 1024
MAX_LAYERS = 64
MAX_BASE_UNPACKED_BYTES = 2 * 1024 * 1024 * 1024
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_CONFIG = "application/vnd.oci.image.config.v1+json"
OCI_LAYER = "application/vnd.oci.image.layer.v1.tar+gzip"
DOCKER_MANIFEST = "application/vnd.docker.distribution.manifest.v2+json"
DOCKER_CONFIG = "application/vnd.docker.container.image.v1+json"
DOCKER_LAYER = "application/vnd.docker.image.rootfs.diff.tar.gzip"

# Fixed service wiring for this release profile. Content is user-generated;
# deployment/network authority and startup commands are not.
NGINX_CONFIG = b"""pid /tmp/nginx.pid;
events { worker_connections 256; }
http {
    include /etc/nginx/mime.types;
    default_type application/octet-stream;
    access_log /dev/stdout;
    error_log /dev/stderr warn;
    client_body_temp_path /tmp/client_temp;
    proxy_temp_path /tmp/proxy_temp;
    fastcgi_temp_path /tmp/fastcgi_temp;
    uwsgi_temp_path /tmp/uwsgi_temp;
    scgi_temp_path /tmp/scgi_temp;
    server {
        listen 8080;
        root /usr/share/nginx/html;
        location /api/ {
            proxy_pass http://backend:8080;
            proxy_set_header Host $host;
            proxy_set_header X-Forwarded-Proto $scheme;
        }
        location / { try_files $uri $uri/ /index.html; }
    }
}
"""


class ImagePackagingError(ValueError):
    pass


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode()


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ImagePackagingError("Image metadata has duplicate fields")
        result[key] = value
    return result


def _metadata(body: bytes) -> dict:
    if not isinstance(body, bytes) or not 1 <= len(body) <= MAX_METADATA_BYTES:
        raise ImagePackagingError("Image metadata exceeds bounds")
    try:
        value = json.loads(body, object_pairs_hook=_object,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (UnicodeError, ValueError, RecursionError):
        raise ImagePackagingError("Image metadata is invalid") from None
    if not isinstance(value, dict):
        raise ImagePackagingError("Image metadata must be an object")
    return value


def _descriptor(value: dict, body: bytes, allowed_media: set[str]):
    if (not isinstance(value, dict) or set(value) != {"mediaType", "digest", "size"}
            or value["mediaType"] not in allowed_media
            or type(value["size"]) is not int or value["size"] != len(body)
            or value["digest"] != digest(body)):
        raise ImagePackagingError("Runtime image blob does not match its pinned descriptor")


def _layer_diff_id(body: bytes, remaining: int) -> tuple[str, int]:
    size = 0
    checksum = hashlib.sha256()
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(body)) as stream:
            while block := stream.read(1024 * 1024):
                size += len(block)
                if size > remaining:
                    raise ImagePackagingError("Runtime image unpacked layers exceed bounds")
                checksum.update(block)
    except (OSError, EOFError):
        raise ImagePackagingError("Runtime image layer compression is invalid") from None
    return "sha256:" + checksum.hexdigest(), size


@dataclass(frozen=True)
class ApprovedRuntimeBase:
    """Constructed only from a separately authenticated operator approval.

    This object is not itself evidence of operator authority. The calling builder
    must obtain the pin/role from an immutable target, never model output.
    """
    role: Literal["nginx_static_v1", "temurin_java17_v1"]
    approved_manifest_digest: str
    manifest: bytes
    config: bytes
    layers: tuple[bytes, ...]

    def inspect(self) -> tuple[dict, dict]:
        if (self.role not in {"nginx_static_v1", "temurin_java17_v1"}
                or not DIGEST.fullmatch(self.approved_manifest_digest)
                or digest(self.manifest) != self.approved_manifest_digest
                or not isinstance(self.layers, tuple)
                or not 1 <= len(self.layers) <= MAX_LAYERS
                or any(not isinstance(body, bytes) or not body for body in self.layers)
                or sum(map(len, self.layers)) > MAX_BASE_BYTES):
            raise ImagePackagingError("Runtime image approval or size is invalid")
        manifest = _metadata(self.manifest)
        config = _metadata(self.config)
        if (set(manifest) != {"schemaVersion", "mediaType", "config", "layers"}
                or type(manifest["schemaVersion"]) is not int or manifest["schemaVersion"] != 2
                or manifest["mediaType"] not in {OCI_MANIFEST, DOCKER_MANIFEST}
                or not isinstance(manifest["layers"], list)
                or len(manifest["layers"]) != len(self.layers)):
            raise ImagePackagingError("Runtime image manifest is unsupported")
        _descriptor(manifest["config"], self.config, {OCI_CONFIG, DOCKER_CONFIG})
        for descriptor, body in zip(manifest["layers"], self.layers):
            _descriptor(descriptor, body, {OCI_LAYER, DOCKER_LAYER})
        rootfs = config.get("rootfs")
        runtime = config.get("config")
        if (config.get("os") != "linux" or config.get("architecture") != "amd64"
                or not isinstance(runtime, dict) or runtime.get("OnBuild") not in (None, [])
                or not isinstance(rootfs, dict) or set(rootfs) != {"type", "diff_ids"}
                or rootfs["type"] != "layers" or not isinstance(rootfs["diff_ids"], list)
                or len(rootfs["diff_ids"]) != len(self.layers)
                or any(not isinstance(item, str) or not DIGEST.fullmatch(item)
                       for item in rootfs["diff_ids"])):
            raise ImagePackagingError("Runtime image platform, hooks or rootfs are invalid")
        history = config.get("history", [])
        if not isinstance(history, list) or len(history) > 256:
            raise ImagePackagingError("Runtime image history exceeds bounds")
        environment = runtime.get("Env", [])
        if (not isinstance(environment, list) or len(environment) > 128
                or any(not isinstance(item, str) or len(item) > 2048
                       or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", item)
                       for item in environment)):
            raise ImagePackagingError("Runtime image environment is invalid")
        unpacked = 0
        for body, expected in zip(self.layers, rootfs["diff_ids"]):
            actual, size = _layer_diff_id(body, MAX_BASE_UNPACKED_BYTES - unpacked)
            unpacked += size
            if actual != expected:
                raise ImagePackagingError("Runtime image layer differs from rootfs evidence")
        return manifest, config


@dataclass(frozen=True)
class PackagedImage:
    profile: str
    source_hash: str
    claim_id: str
    validation_job_id: str
    artifact_sha256: str
    base_manifest_digest: str
    manifest: bytes
    config: bytes
    layer: bytes
    base_layers: tuple[bytes, ...]

    @property
    def manifest_digest(self) -> str:
        return digest(self.manifest)

    @property
    def evidence(self) -> dict:
        return {
            "schema": "gca-oci-artifact-build-v1", "profile": self.profile,
            "source_hash": self.source_hash, "claim_id": self.claim_id,
            "validation_job_id": self.validation_job_id,
            "artifact_sha256": self.artifact_sha256,
            "base_manifest_digest": self.base_manifest_digest,
            "image_manifest_digest": self.manifest_digest,
            "image_config_digest": digest(self.config), "output_layer_digest": digest(self.layer),
            "generated_code_executed": False,
        }

    @property
    def evidence_hash(self) -> str:
        return hashlib.sha256(canonical(self.evidence)).hexdigest()

    def layout_archive(self) -> bytes:
        """OCI layout ready for a separate authenticated import/publish controller."""
        blobs = {digest(body): body for body in (*self.base_layers, self.layer,
                                               self.config, self.manifest)}
        files = {"oci-layout": canonical({"imageLayoutVersion": "1.0.0"}),
                 "index.json": canonical({"schemaVersion": 2, "manifests": [{
                     "mediaType": OCI_MANIFEST, "digest": self.manifest_digest,
                     "size": len(self.manifest),
                     "platform": {"architecture": "amd64", "os": "linux"},
                 }]})}
        files.update({"blobs/sha256/" + key.split(":", 1)[1]: body
                      for key, body in blobs.items()})
        return _tar(files)


def _tar(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name, body in sorted(files.items()):
            item = tarfile.TarInfo(name)
            item.size = len(body)
            item.mode = 0o644
            item.uid = item.gid = 65532
            item.mtime = 0
            archive.addfile(item, io.BytesIO(body))
    return output.getvalue()


def _java17_class_headers(jar: bytes):
    """Reject known-incompatible application class versions, not prove JVM behavior."""
    with zipfile.ZipFile(io.BytesIO(jar)) as archive:
        classes = [item for item in archive.infolist()
                   if item.filename.startswith("BOOT-INF/classes/") and item.filename.endswith(".class")]
        for item in classes:
            with archive.open(item) as stream:
                header = stream.read(8)
            if (len(header) != 8 or header[:4] != b"\xca\xfe\xba\xbe"
                    or not 45 <= int.from_bytes(header[6:8], "big") <= 61
                    or int.from_bytes(header[4:6], "big") == 65535):
                raise ImagePackagingError("Application class header is incompatible with Java 17 runtime")


def package_validated_artifact(base: ApprovedRuntimeBase, payload: bytes, reference: dict,
                               *, profile: str, source_hash: str, claim_id: str,
                               job_id: str, now: int | None = None) -> PackagedImage:
    """Bind actual validated bytes and approved runtime to a measured OCI image."""
    verified = verify_artifact_reference(reference, profile=profile, source_hash=source_hash,
                                         claim_id=claim_id, job_id=job_id, now=now)
    artifact = inspect_build_artifact(payload, profile)
    file_set = hashlib.sha256(json.dumps(artifact.file_hashes, separators=(",", ":")).encode())
    if (artifact.archive_sha256 != verified["archive_sha256"]
            or file_set.hexdigest() != verified["file_set_sha256"]
            or artifact.file_count != verified["file_count"]
            or artifact.content_bytes != verified["content_bytes"]):
        raise ImagePackagingError("Build output bytes do not match retained validation evidence")
    expected_role = "nginx_static_v1" if profile == "react_build" else "temurin_java17_v1"
    if base.role != expected_role:
        raise ImagePackagingError("Runtime image role does not match validated output")
    base_manifest, config = base.inspect()
    files = {}
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
        for member in archive:
            if member.isfile():
                if any(part.startswith(".wh.") for part in member.name.split("/")):
                    raise ImagePackagingError("Generated output cannot introduce layer whiteouts")
                name = ("usr/share/nginx/html/" + member.name[len("dist/"):]
                        if profile == "react_build" else "app/app.jar")
                files[name] = archive.extractfile(member).read()
    if profile == "react_build":
        files["etc/nginx/nginx.conf"] = NGINX_CONFIG
        entrypoint = ["/usr/sbin/nginx"]
        command = ["-g", "daemon off;"]
    else:
        _java17_class_headers(files["app/app.jar"])
        entrypoint = ["/opt/java/openjdk/bin/java"]
        command = ["-jar", "/app/app.jar"]
    uncompressed = _tar(files)
    layer = gzip.compress(uncompressed, compresslevel=6, mtime=0)
    runtime = config["config"]
    # Preserve approved runtime env (e.g. JAVA_HOME), never generated startup authority.
    runtime.update({"User": "65532:65532", "Entrypoint": entrypoint, "Cmd": command,
                    "WorkingDir": "/app" if profile == "spring_package" else "/",
                    "ExposedPorts": {"8080/tcp": {}}, "OnBuild": [], "Volumes": {}})
    runtime.pop("Healthcheck", None)
    runtime.pop("Shell", None)
    labels = runtime.get("Labels") or {}
    if not isinstance(labels, dict) or any(not isinstance(key, str) or not isinstance(value, str)
                                           for key, value in labels.items()):
        raise ImagePackagingError("Runtime image labels are invalid")
    runtime["Labels"] = {**labels, "gca.source_hash": source_hash,
                         "gca.validation_claim": claim_id,
                         "gca.artifact_sha256": artifact.archive_sha256}
    config["rootfs"]["diff_ids"].append(digest(uncompressed))
    config["history"] = config.get("history", []) + [
        {"created_by": "gca fixed validated-artifact packager v1", "empty_layer": False},
    ]
    config.pop("created", None)
    config.pop("container", None)
    config.pop("container_config", None)
    config_bytes = canonical(config)
    # OCI and Docker gzip layers have the same content encoding; normalize media type.
    layers = [{**item, "mediaType": OCI_LAYER} for item in base_manifest["layers"]]
    layers.append({"mediaType": OCI_LAYER, "digest": digest(layer), "size": len(layer)})
    manifest = canonical({"schemaVersion": 2, "mediaType": OCI_MANIFEST,
                          "config": {"mediaType": OCI_CONFIG, "digest": digest(config_bytes),
                                     "size": len(config_bytes)}, "layers": layers})
    return PackagedImage(profile, source_hash, claim_id, job_id, artifact.archive_sha256,
                         base.approved_manifest_digest, manifest, config_bytes, layer, base.layers)
