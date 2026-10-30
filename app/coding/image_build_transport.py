"""Fixed pinned IAP transport to the reviewed packaging gateway, not a model tool."""
from datetime import datetime
import hashlib
import json
import re

from app.coding import image_output_store as outputs, oci_packager as oci
from app.coding import validation_transport as transport


def _binding(host, binding, *, cleanup=False):
    try:
        verified = outputs.verify_binding(binding, allow_expired=cleanup)
    except outputs.ImageOutputError:
        raise transport.ValidationTransportError("invalid_packaging_binding") from None
    if verified["server_uuid"] != host.server_uuid or verified["project_id"] != host.project_id:
        raise transport.ValidationTransportError("packaging_host_binding_mismatch")
    return verified


def verify_measurement(binding, result):
    """Structure/binding only: origin trust comes from pinned transport, not JSON."""
    fields = {"build_job_id", "binding_hash", "output_archive_sha256", "output_archive_bytes",
              "output_expires_at_unix", "completed_at", "evidence", "packaging_job_id",
              "execution_cleanup_verified"}
    expected = {key: binding[key] for key in ("profile", "source_hash", "validation_job_id", "artifact_sha256",
                                             "base_manifest_digest")}
    expected.update(schema="gca-oci-artifact-build-v1", claim_id=binding["validation_claim_id"],
                    generated_code_executed=False)
    if (not isinstance(result, dict) or set(result) != fields
            or result["build_job_id"] != binding["build_job_id"]
            or result["binding_hash"] != hashlib.sha256(oci.canonical(binding)).hexdigest()
            or result["execution_cleanup_verified"] is not True
            or not isinstance(result["packaging_job_id"], str)
            or not re.fullmatch(r"[a-f0-9]{32}", result["packaging_job_id"])
            or not isinstance(result["output_archive_sha256"], str)
            or not re.fullmatch(r"[a-f0-9]{64}", result["output_archive_sha256"])
            or type(result["output_archive_bytes"]) is not int
            or not 0 < result["output_archive_bytes"] <= outputs.MAX_OUTPUT_BYTES
            or type(result["output_expires_at_unix"]) is not int
            or result["output_expires_at_unix"] != binding["output_expires_at_unix"]
            or not isinstance(result["completed_at"], str)
            or not isinstance(result["evidence"], dict)):
        raise transport.ValidationTransportError("packaging_measurement_binding_mismatch")
    evidence = result["evidence"]
    hashes = {"image_manifest_digest", "image_config_digest", "output_layer_digest"}
    if (set(evidence) != set(expected) | hashes
            or any(evidence.get(key) != value for key, value in expected.items())
            or evidence["generated_code_executed"] is not False
            or any(not isinstance(evidence[key], str) or not oci.DIGEST.fullmatch(evidence[key]) for key in hashes)):
        raise transport.ValidationTransportError("packaging_image_evidence_mismatch")
    try:
        completed = datetime.fromisoformat(result["completed_at"])
        if completed.utcoffset() is None or completed.timestamp() >= binding["execution_deadline_unix"]:
            raise ValueError("completion outside execution authority")
    except ValueError:
        raise transport.ValidationTransportError("packaging_completion_time_mismatch") from None
    return result


async def _exchange(host, operation, binding, **fields):
    frame = json.dumps({"operation": operation, "binding": binding, **fields},
                       separators=(",", ":")).encode() + b"\n"
    if len(frame) > 16384:
        raise transport.ValidationTransportError("packaging_header_limit")
    return await transport._exchange(host, frame, binding["timeout_seconds"] + 90 if operation == "assemble"
                                     else 45, gateway="packaging")


async def assemble(host, binding, validation_binding, reference):
    binding = _binding(host, binding)
    if (not isinstance(validation_binding, dict)
            or validation_binding.get("server_uuid") != host.server_uuid
            or validation_binding.get("claim_id") != binding["validation_claim_id"]
            or validation_binding.get("source_hash") != binding["source_hash"]
            or validation_binding.get("profile") != binding["profile"]):
        raise transport.ValidationTransportError("packaging_validation_binding_mismatch")
    result = await _exchange(host, "assemble", binding, validation_binding=validation_binding, reference=reference)
    return verify_measurement(binding, result)


async def recover(host, binding):
    binding = _binding(host, binding)
    return verify_measurement(binding, await _exchange(host, "recover", binding))


async def cleanup(host, binding):
    binding = _binding(host, binding, cleanup=True)
    result = await _exchange(host, "cleanup", binding)
    return verify_cleanup(binding, result)


def verify_cleanup(binding, result):
    """Cleanup structure/binding only; the caller still needs pinned origin trust."""
    fields = {"build_job_id", "packaging_job_id", "binding_hash", "execution_cleanup_verified",
              "output_removed", "staging_removed", "observed_at"}
    if (not isinstance(result, dict) or set(result) != fields
            or result["build_job_id"] != binding["build_job_id"]
            or result["binding_hash"] != hashlib.sha256(oci.canonical(binding)).hexdigest()
            or not isinstance(result["packaging_job_id"], str)
            or not re.fullmatch(r"[a-f0-9]{32}", result["packaging_job_id"])
            or any(result[key] is not True for key in ("execution_cleanup_verified", "output_removed",
                                                      "staging_removed"))
            or not isinstance(result["observed_at"], str)):
        raise transport.ValidationTransportError("packaging_cleanup_binding_mismatch")
    try:
        if datetime.fromisoformat(result["observed_at"]).utcoffset() is None:
            raise ValueError("cleanup time lacks zone")
    except ValueError:
        raise transport.ValidationTransportError("packaging_cleanup_time_mismatch") from None
    return result
