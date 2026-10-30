from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import io
import os
from pathlib import Path
import tarfile
import tempfile
from types import SimpleNamespace

import pytest

from app.coding import scanner_database as db, oci_packager as oci
from app.coding import runtime_base_acquisition as cas
from scripts import prepare_scanner_databases as cli


NOW = datetime(2026, 10, 13, 5, tzinfo=timezone.utc)


def archive(contents):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as tar:
        for name, body, kind in contents:
            item = tarfile.TarInfo(name); item.size = len(body); item.type = kind
            tar.addfile(item, io.BytesIO(body))
    return output.getvalue()


@pytest.fixture
def target(monkeypatch):
    body = b"fixed-non-executable-database-content" * 7
    metadata = oci.canonical({"Version": 2, "UpdatedAt": (NOW-timedelta(hours=1)).isoformat(),
                              "NextUpdate": (NOW+timedelta(hours=23)).isoformat(),
                              "DownloadedAt": "0001-01-01T00:00:00Z"})
    packed = archive([("trivy.db", body, tarfile.REGTYPE), ("metadata.json", metadata, tarfile.REGTYPE)])
    pin = {**db.CATALOG["vulnerability"], "archive_digest": oci.digest(packed),
           "archive_bytes": len(packed), "database_bytes": len(body)}
    manifest = oci.canonical({"schemaVersion": 2, "mediaType": oci.OCI_MANIFEST,
        "artifactType": "application/vnd.aquasec.trivy.config.v1+json",
        "config": {"mediaType": "application/vnd.oci.empty.v1+json", "digest": db.EMPTY_CONFIG,
                   "size": 2, "data": "e30="},
        "layers": [{"mediaType": pin["layer_media"], "digest": pin["archive_digest"],
                    "size": pin["archive_bytes"], "annotations": {"org.opencontainers.image.title": "db.tar.gz"}}],
        "annotations": {"org.opencontainers.image.created": NOW.isoformat()}})
    pin.update(manifest_digest=oci.digest(manifest), manifest_bytes=len(manifest))
    monkeypatch.setattr(db, "CATALOG", {"vulnerability": pin})
    class Registry:
        calls = 0
        manifest_calls = 0
        interrupt = False
        wrong_range = False
        def manifest(self, checksum):
            self.manifest_calls += 1
            assert checksum == pin["manifest_digest"]
            return manifest
        @contextmanager
        def blob(self, checksum, *, offset=0):
            assert checksum == pin["archive_digest"]
            self.calls += 1
            result = io.BytesIO(packed[offset:]); result.status = 206 if offset else 200
            result.headers = {"Content-Range": "wrong" if self.wrong_range else f"bytes {offset}-{len(packed)-1}/{len(packed)}"}
            if self.interrupt:
                read = result.read; calls = 0
                def fail(size):
                    nonlocal calls
                    calls += 1
                    if calls == 1: return read(min(size, 7))
                    raise TimeoutError("synthetic interruption")
                result.read = fail
            yield result
    with tempfile.TemporaryDirectory(prefix=".gca-db-test-", dir=Path.home()) as directory:
        root = Path(directory); root.chmod(0o700)
        yield SimpleNamespace(root=root, body=body, metadata=metadata, packed=packed, pin=pin,
                              manifest=manifest, registry=Registry())


def test_complete_pinned_data_acquisition_is_not_a_scan_or_approval(target):
    result = db.acquire_database(target.root, "vulnerability", target.registry, now=NOW)
    assert result["database_digest"] == oci.digest(target.body)
    assert result["metadata_digest"] == oci.digest(target.metadata)
    for key in ("publisher_signature_verified", "database_opened_by_scanner", "scan_completed", "runtime_approved"):
        assert result[key] is False
    assert (target.root/"vulnerability/trivy.db").read_bytes() == target.body
    assert all(not x.stat().st_mode & 0o077 for x in target.root.rglob("*"))
    assert db.acquire_database(target.root, "vulnerability", target.registry, now=NOW) == result
    assert target.registry.calls == 1


def test_interrupted_archive_resumes_only_exact_range(target):
    path = target.root/"archive.tar.gz"
    target.registry.interrupt = True
    with pytest.raises(TimeoutError): db.acquire_archive(path, target.pin, target.registry)
    partial = path.with_name(path.name+".partial")
    assert partial.stat().st_size == 7 and not path.exists()
    target.registry.interrupt = False; target.registry.wrong_range = True
    with pytest.raises(db.DatabaseAcquisitionError, match="range"):
        db.acquire_archive(path, target.pin, target.registry)
    assert partial.read_bytes() == target.packed[:7]
    target.registry.wrong_range = False
    db.acquire_archive(path, target.pin, target.registry)
    assert path.read_bytes() == target.packed


def test_complete_partial_archive_reconciles_without_network(target):
    path=target.root/"archive.tar.gz"; pending=path.with_name(path.name+".partial")
    pending.write_bytes(target.packed); pending.chmod(0o600)
    db.acquire_archive(path, target.pin, target.registry)
    assert target.registry.calls == 0 and path.read_bytes() == target.packed


def test_expanded_database_prefix_is_compared_before_continuation(target):
    directory=target.root/"vulnerability"; directory.mkdir(mode=0o700)
    (directory/"archive.tar.gz").write_bytes(target.packed); (directory/"archive.tar.gz").chmod(0o600)
    path=directory/"trivy.db.partial"; path.write_bytes(target.body[:7]); path.chmod(0o600)
    result=db.unpack_database(directory,"vulnerability",now=NOW)
    assert result["database_digest"] == oci.digest(target.body)
    assert not path.exists() and (directory/"trivy.db").read_bytes()==target.body


@pytest.mark.parametrize("fault", ["prefix", "symlink", "hardlink", "public", "executable"])
def test_invalid_retained_database_is_not_promoted(target,fault):
    directory=target.root/"vulnerability"; directory.mkdir(mode=0o700)
    packed=directory/"archive.tar.gz"; packed.write_bytes(target.packed); packed.chmod(0o600)
    path=directory/"trivy.db.partial"; path.write_bytes(target.body[:7]); path.chmod(0o600)
    if fault=="prefix":path.write_bytes(b"corrupt")
    elif fault=="symlink":other=directory/"other";path.rename(other);path.symlink_to(other)
    elif fault=="hardlink":os.link(path,directory/"other")
    elif fault=="public":path.chmod(0o644)
    else:path.chmod(0o700)
    with pytest.raises((db.DatabaseAcquisitionError,cas.RuntimeAcquisitionError)):
        db.unpack_database(directory,"vulnerability",now=NOW)
    assert not (directory/"trivy.db").exists()


def test_freshness_is_rechecked_not_taken_from_saved_receipt(target):
    db.acquire_database(target.root,"vulnerability",target.registry,now=NOW)
    with pytest.raises(db.DatabaseAcquisitionError,match="stale"):
        db.observe_database(target.root,"vulnerability",now=NOW+timedelta(days=2))


@pytest.mark.parametrize("changes", [{"Version":1},{"Version":True},{"UpdatedAt":NOW.replace(tzinfo=None).isoformat()},
    {"UpdatedAt":(NOW+timedelta(minutes=6)).isoformat()}, {"NextUpdate":NOW.isoformat()},
    {"NextUpdate":(NOW+timedelta(days=100)).isoformat()},{"extra":"authority"}])
def test_invalid_metadata_never_gets_freshness_authority(target,changes):
    metadata={**oci._metadata(target.metadata),**changes}
    with pytest.raises(db.DatabaseAcquisitionError):db.validate_metadata(oci.canonical(metadata),"vulnerability",NOW)


def test_pax_extension_is_rejected_before_its_body_is_read():
    item=tarfile.TarInfo("pax");item.type=tarfile.XHDTYPE;item.size=1024**3
    source=io.BytesIO(item.tobuf())
    with pytest.raises(db.DatabaseAcquisitionError,match="fixed regular"):
        next(db.fixed_members(source,{"database_name":"trivy.db","database_bytes":100}))
    assert source.tell()==512


def test_capacity_is_checked_before_network(target,monkeypatch):
    monkeypatch.setattr(db,"MAX_STORE",1)
    with pytest.raises(db.DatabaseAcquisitionError,match="capacity"):
        db.acquire_database(target.root,"vulnerability",target.registry,now=NOW)
    assert target.registry.calls==0 and target.registry.manifest_calls==0


def test_operator_source_is_bounded_and_compiles():
    source=cli.program()
    assert len(source.encode())<100000
    compile(source,"operator-database-controller","exec")


def test_forged_scan_or_approval_evidence_is_rejected():
    request={"operation":"probe"}
    for key in ("scan_completed","runtime_approved"):
        result={"operation":"probe","journal_available":True,"scan_completed":False,"runtime_approved":False,"receipts":[],key:True}
        with pytest.raises(RuntimeError):cli.verify(request,result)
