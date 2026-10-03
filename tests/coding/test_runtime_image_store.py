import json
from pathlib import Path
import tempfile

import pytest

from app.coding.oci_packager import ImagePackagingError, canonical, digest
from app.coding.runtime_image_store import load_runtime_base
from tests.coding.test_oci_packager import base


def write_store(root):
    runtime = base()
    layout = root / "layout"
    blobs = layout / "blobs" / "sha256"
    blobs.mkdir(parents=True, mode=0o700)
    for body in (runtime.manifest, runtime.config, *runtime.layers):
        path = blobs / digest(body).split(':')[1]
        path.write_bytes(body)
        path.chmod(0o600)
    (layout / "oci-layout").write_bytes(canonical({"imageLayoutVersion": "1.0.0"}))
    (layout / "index.json").write_bytes(canonical({"schemaVersion": 2, "manifests": [{
        "digest": runtime.approved_manifest_digest, "size": len(runtime.manifest),
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
    }]}))
    policy = root / "policy.json"
    policy.write_bytes(canonical({"version": 1, "roles": {"nginx_static_v1": {
        "manifest_digest": runtime.approved_manifest_digest, "layout_directory": str(layout),
    }}}))
    policy.chmod(0o600)
    workspace = root / "generated-workspace"
    workspace.mkdir(mode=0o700)
    return runtime, policy, workspace


def test_loads_only_operator_pinned_role_and_rehashes_actual_blobs():
    with tempfile.TemporaryDirectory(prefix=".gca-runtime-store-", dir=Path.home()) as folder:
        expected, policy, workspace = write_store(Path(folder))
        assert load_runtime_base(policy, role="nginx_static_v1", workspace=workspace) == expected
        with pytest.raises(ImagePackagingError, match="No operator-approved"):
            load_runtime_base(policy, role="temurin_java17_v1", workspace=workspace)


@pytest.mark.parametrize("change", ["world_writable", "model_policy", "symlink", "wrong_blob", "index"])
def test_model_editable_or_changed_runtime_store_is_rejected(change):
    with tempfile.TemporaryDirectory(prefix=".gca-runtime-store-", dir=Path.home()) as folder:
        root = Path(folder)
        runtime, policy, workspace = write_store(root)
        if change == "world_writable":
            policy.chmod(0o666)
        elif change == "model_policy":
            moved = workspace / "policy.json"
            policy.rename(moved)
            policy = moved
        elif change == "symlink":
            alias = root / "alias.json"
            alias.symlink_to(policy)
            policy = alias
        elif change == "index":
            (root / "layout/index.json").write_text(json.dumps({"schemaVersion": 2, "manifests": []}))
        else:
            path = root / "layout/blobs/sha256" / digest(runtime.layers[0]).split(':')[1]
            body = path.read_bytes()
            path.write_bytes(body[:-1] + bytes([body[-1] ^ 1]))
        with pytest.raises(ImagePackagingError):
            load_runtime_base(policy, role="nginx_static_v1", workspace=workspace)
