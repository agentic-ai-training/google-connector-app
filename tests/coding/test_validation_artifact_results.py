from datetime import datetime, timedelta, timezone
import time
from uuid import uuid4

import pytest

from app.coding import deployment_admission as deployment
from app.coding import validation_admission as admission
from app.coding import validation_controller as controller


def result_case():
    request = admission.ValidationRequest(
        run_id=uuid4(), sequence_no=0, target_id="test-target", target_hash="a" * 64,
        source_hash="b" * 64, image_id="sha256:" + "c" * 64, profile="react_build",
        timeout_seconds=10, planned_end_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    claim_id = uuid4()
    job_id = "d" * 32
    reference = {
        "artifact_id": job_id + "-" + "e" * 64 + ".tar", "archive_sha256": "e" * 64,
        "file_set_sha256": "f" * 64, "file_count": 1, "content_bytes": 10,
        "profile": request.profile, "validation_job_id": job_id,
        "claim_id": str(claim_id), "source_hash": request.source_hash,
        "expires_at_unix": int(time.time()) + 3600,
    }
    result = {
        "claim_id": str(claim_id), "request_hash": deployment.fingerprint(request),
        "source_hash": request.source_hash, "image_id": request.image_id,
        "profile": request.profile, "network": "none", "isolation": "gvisor",
        "cleanup_verified": True, "workspace_mutations_discarded": True,
        "success": True, "exit_code": 0, "validation_job_id": job_id,
        "stdout": "", "stderr": "", "build_artifact": reference,
    }
    return request, claim_id, result


def test_controller_retains_verified_artifact_reference():
    request, claim_id, result = result_case()
    verified = controller._verified_result(request, claim_id, result)
    assert verified["build_artifact"] == result["build_artifact"]


@pytest.mark.parametrize("field,value", [
    ("source_hash", "a" * 64), ("claim_id", str(uuid4())),
    ("validation_job_id", "c" * 32), ("content_bytes", True),
    ("expires_at_unix", 0), ("file_count", 0), ("profile", "spring_package"),
])
def test_controller_rejects_unbound_or_expired_artifact(field, value):
    request, claim_id, result = result_case()
    result["build_artifact"][field] = value
    with pytest.raises(admission.ValidationConflict, match="artifact"):
        controller._verified_result(request, claim_id, result)


def test_controller_cannot_record_build_success_without_artifact():
    request, claim_id, result = result_case()
    del result["build_artifact"]
    with pytest.raises(admission.ValidationConflict, match="artifact"):
        controller._verified_result(request, claim_id, result)
