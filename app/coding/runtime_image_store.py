"""Read bounded operator-owned OCI runtime bases, never model-chosen image pins."""
from __future__ import annotations

import os
from pathlib import Path
import stat

from app.coding.oci_packager import (
    ApprovedRuntimeBase, DIGEST, ImagePackagingError, MAX_BASE_BYTES, MAX_LAYERS,
    MAX_METADATA_BYTES, _metadata, digest,
)


def _protected(path: Path, workspace: Path):
    if not path.is_absolute() or path == workspace or workspace in path.parents:
        raise ImagePackagingError("Runtime image authority must be outside the generated workspace")
    for entry in (path, *path.parents):
        info = entry.lstat()
        if (entry.is_symlink() or info.st_uid not in {0, os.geteuid()}
                or info.st_mode & 0o022):
            raise ImagePackagingError("Runtime image store is not operator protected")


def _read(path: Path, maximum: int, workspace: Path, *, private=False) -> bytes:
    _protected(path, workspace)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or not 1 <= info.st_size <= maximum
                or info.st_uid not in {0, os.geteuid()}
                or info.st_mode & (0o077 if private else 0o022)):
            raise ImagePackagingError("Runtime image file identity or size is invalid")
        body = stream.read(maximum + 1)
        if len(body) != info.st_size:
            raise ImagePackagingError("Runtime image file changed during read")
    return body


def load_runtime_base(policy_path: Path, *, role: str, workspace: Path) -> ApprovedRuntimeBase:
    """Internal controller API. The policy path is trusted configuration, not request text.

    Policy v1 maps approved runtime roles to one manifest digest and an OCI layout
    directory. All descriptors and bytes are reverified; no extraction/download.
    """
    workspace = workspace.resolve(strict=True)
    if role not in {"nginx_static_v1", "temurin_java17_v1"}:
        raise ImagePackagingError("Unsupported runtime role")
    policy = _metadata(_read(policy_path, 32768, workspace, private=True))
    if (set(policy) != {"version", "roles"} or type(policy["version"]) is not int
            or policy["version"] != 1 or not isinstance(policy["roles"], dict)
            or not 1 <= len(policy["roles"]) <= 2
            or set(policy["roles"]) - {"nginx_static_v1", "temurin_java17_v1"}):
        raise ImagePackagingError("Runtime image policy is invalid")
    selected = policy["roles"].get(role)
    if (not isinstance(selected, dict) or set(selected) != {"manifest_digest", "layout_directory"}
            or not isinstance(selected["manifest_digest"], str)
            or not DIGEST.fullmatch(selected["manifest_digest"])
            or not isinstance(selected["layout_directory"], str)):
        raise ImagePackagingError("No operator-approved runtime for this role")
    layout = Path(selected["layout_directory"])
    _protected(layout, workspace)
    metadata = _metadata(_read(layout / "oci-layout", 1024, workspace))
    if metadata != {"imageLayoutVersion": "1.0.0"}:
        raise ImagePackagingError("Runtime image layout is unsupported")
    blobs = layout / "blobs" / "sha256"

    def read_blob(checksum: str, maximum: int) -> bytes:
        if not isinstance(checksum, str) or not DIGEST.fullmatch(checksum):
            raise ImagePackagingError("Runtime image blob digest is invalid")
        body = _read(blobs / checksum.split(":", 1)[1], maximum, workspace)
        if digest(body) != checksum:
            raise ImagePackagingError("Runtime image blob content changed")
        return body

    manifest_bytes = read_blob(selected["manifest_digest"], MAX_METADATA_BYTES)
    index = _metadata(_read(layout / "index.json", MAX_METADATA_BYTES, workspace))
    entries = index.get("manifests")
    if (type(index.get("schemaVersion")) is not int or index["schemaVersion"] != 2
            or not isinstance(entries, list) or not 1 <= len(entries) <= 64):
        raise ImagePackagingError("Runtime image index is invalid")
    matching = [entry for entry in entries if isinstance(entry, dict)
                and entry.get("digest") == selected["manifest_digest"]]
    if (len(matching) != 1 or type(matching[0].get("size")) is not int
            or matching[0]["size"] != len(manifest_bytes)):
        raise ImagePackagingError("Runtime image index does not bind the approved manifest")
    manifest = _metadata(manifest_bytes)
    config = manifest.get("config")
    layers = manifest.get("layers")
    if (not isinstance(config, dict) or not isinstance(layers, list)
            or not 1 <= len(layers) <= MAX_LAYERS
            or any(not isinstance(item, dict) or type(item.get("size")) is not int
                   or not 1 <= item["size"] <= MAX_BASE_BYTES for item in layers)
            or sum(item["size"] for item in layers) > MAX_BASE_BYTES):
        raise ImagePackagingError("Runtime image descriptors exceed bounds")
    config_bytes = read_blob(config.get("digest"), MAX_METADATA_BYTES)
    bodies = tuple(read_blob(item.get("digest"), item["size"]) for item in layers)
    base = ApprovedRuntimeBase(role, selected["manifest_digest"], manifest_bytes, config_bytes, bodies)
    base.inspect()
    return base
