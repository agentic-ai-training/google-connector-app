"""Acquire a pinned public scanner as DATA; verify before permitting execution.

The CLI is operator-only. No release tag, download URL, certificate identity or
shell command is supplied by a model. Acquisition, signature verification and
extracting a non-executable binary do not approve a runtime or constitute a scan.
"""
from contextlib import contextmanager
import hashlib
import io
import os
from pathlib import Path
import subprocess  # nosec B404
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request

from app.coding import runtime_base_acquisition as cas, oci_packager as oci


VERSION = "0.75.0"
VERIFIER_VERSION = "4.5.0"
IDENTITY = "https://github.com/aquasecurity/trivy/.github/workflows/reusable-release.yaml@refs/tags/v0.75.0"
ISSUER = "https://token.actions.githubusercontent.com"
ASSETS = {
    "trivy_0.75.0_Linux-64bit.tar.gz": {
        "size": 51_704_231,
        "digest": "sha256:c6e65abddb348e25f10549df887045629cf28cc72453cd1c63acb717316b3f3f",
    },
    "trivy_0.75.0_Linux-64bit.tar.gz.sigstore.json": {
        "size": 10_103,
        "digest": "sha256:3565a49a9d857a977b18cb402323cb71ed806b735730c02a757c46133ddc12c0",
    },
}
ALLOWED_HOSTS = {"github.com", "release-assets.githubusercontent.com"}
MAX_BINARY_BYTES = 176 * 1024**2
BINARY_BYTES = 172_765_344  # Actual regular member in the pinned, signed release.
BINARY_SHA256 = "93f9da8e4ba5e0c1c76d8234ed2494cf9afb0a96fd21953e424bb795f3299b8e"


class ScannerAssetError(RuntimeError):
    pass


def secure_url(value):
    try:
        parsed = urllib.parse.urlsplit(value)
        if (parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS
                or parsed.username or parsed.password or parsed.port not in (None, 443)
                or parsed.fragment):
            raise ValueError("endpoint")
    except (ValueError, TypeError):
        raise ScannerAssetError("scanner endpoint is not allowlisted HTTPS") from None
    return parsed


class PublicRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        secure_url(newurl)
        result = super().redirect_request(request, fp, code, message, headers, newurl)
        if result is not None:
            result.remove_header("Authorization")
            result.remove_header("Cookie")
        return result


class PublicAssets:
    def __init__(self):
        self.deadline = time.monotonic() + 240
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), PublicRedirect())

    @contextmanager
    def blob(self, checksum, *, offset=0):
        names = [name for name, pin in ASSETS.items() if pin["digest"] == checksum]
        if len(names) != 1:
            raise ScannerAssetError("scanner asset is not in the fixed catalog")
        if type(offset) is not int or not 0 <= offset < ASSETS[names[0]]["size"]:
            raise ScannerAssetError("scanner asset range is invalid")
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ScannerAssetError("scanner download deadline exhausted")
        url = "https://github.com/aquasecurity/trivy/releases/download/v" + VERSION + "/" + names[0]
        secure_url(url)
        headers = {"User-Agent": "gca-public-scanner-acquisition-v1"}
        if offset:
            headers["Range"] = "bytes=" + str(offset) + "-"
        try:
            with self.opener.open(urllib.request.Request(url, headers=headers),
                                  timeout=min(20, remaining)) as response:
                secure_url(response.url)
                class BoundedResponse:
                    status, headers = response.status, response.headers
                    def read(inner, maximum):
                        if time.monotonic() >= self.deadline:
                            raise ScannerAssetError("scanner download deadline exhausted")
                        return response.read(maximum)
                yield BoundedResponse()
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ScannerAssetError("scanner public transfer is unconfirmed") from None


def observe_assets(root: Path):
    cas.protected(root)
    measured = {}
    for name, pin in ASSETS.items():
        body = cas.read_file(root / name, pin["size"])
        if oci.digest(body) != pin["digest"]:
            raise ScannerAssetError("scanner asset fingerprint differs")
        measured[name] = pin.copy()
    return {"version": 1, "scanner": "trivy", "scanner_version": VERSION,
            "assets": measured, "signature_verified": False, "scanner_executed": False,
            "scan_completed": False, "runtime_approved": False}


def acquire_assets(root: Path, client=None):
    """Private hash/range-verified immutable files; no extraction or execution."""
    cas.protected(root)
    client = client or PublicAssets()
    for name, pin in ASSETS.items():
        cas.acquire_blob(root / name, pin, client)
    return observe_assets(root)


def extract_binary(archive: bytes) -> bytes:
    """Return only the bounded regular x86-64 ELF; never extract an archive path."""
    if len(archive) != ASSETS["trivy_0.75.0_Linux-64bit.tar.gz"]["size"]:
        raise ScannerAssetError("scanner archive size differs")
    if oci.digest(archive) != ASSETS["trivy_0.75.0_Linux-64bit.tar.gz"]["digest"]:
        raise ScannerAssetError("scanner archive digest differs")
    result = None
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as source:
            for number, member in enumerate(source):
                path = Path(member.name)
                total += member.size
                if (number >= 64 or path.is_absolute() or ".." in path.parts
                        or not member.isdir() and not member.isreg()
                        or member.size < 0 or total > 200 * 1024**2):
                    raise ScannerAssetError("scanner archive member is unsafe or oversized")
                if member.name == "trivy":
                    if (result is not None or not member.isreg() or member.size != BINARY_BYTES
                            or not 20 <= member.size <= MAX_BINARY_BYTES):
                        raise ScannerAssetError("scanner binary member is invalid")
                    stream = source.extractfile(member)
                    if stream is None:
                        raise ScannerAssetError("scanner binary is unavailable")
                    result = stream.read(MAX_BINARY_BYTES + 1)
                    if len(result) != member.size:
                        raise ScannerAssetError("scanner binary size differs")
    except (tarfile.TarError, OSError, EOFError):
        raise ScannerAssetError("scanner archive is invalid") from None
    # ELF64, little-endian, version 1, EM_X86_64. Not yet executable authority.
    if (result is None or result[:7] != b"\x7fELF\x02\x01\x01"
            or result[18:20] != b"\x3e\x00"
            or hashlib.sha256(result).hexdigest() != BINARY_SHA256):
        raise ScannerAssetError("scanner is not the expected Linux AMD64 ELF")
    return result


def verify_signature(root: Path, verifier_python: Path):
    """Real Sigstore verification with exact identity and fresh default TUF trust.

    The interpreter belongs to a separate operator verifier environment. It sees
    public scanner files and an empty private HOME, not application credentials.
    No scanner binary is run. Failure retains public inputs for diagnosis.
    """
    evidence = observe_assets(root)
    verifier_python = verifier_python.absolute()
    # Venv interpreters are normally symlinks; resolve to the protected executable.
    for path in (verifier_python.parent, verifier_python.parent.parent):
        cas.protected(path)
    executable = verifier_python.resolve(strict=True)
    from app.coding.validation_transport import _protected
    _protected(executable)
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ScannerAssetError("operator verifier interpreter is unavailable")
    home = root / "verifier-home"
    home.mkdir(mode=0o700, exist_ok=True)
    cas.protected(home)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1",
           "XDG_CACHE_HOME": str(home / "cache"), "XDG_DATA_HOME": str(home / "data"),
           "XDG_CONFIG_HOME": str(home / "config")}

    def run(args):
        try:
            result = subprocess.run([str(verifier_python), "-I", "-B", *args],  # nosec B603
                                    env=env, capture_output=True, timeout=120, check=False)
        except (subprocess.TimeoutExpired, OSError):
            raise ScannerAssetError("scanner signature verification is unconfirmed") from None
        if result.returncode or len(result.stdout) + len(result.stderr) > 32768:
            raise ScannerAssetError("scanner signature verification failed")
        return result

    version = run(["-c", "from importlib.metadata import version;print(version('sigstore'))"])
    if version.stdout.strip() != VERIFIER_VERSION.encode():
        raise ScannerAssetError("Sigstore verifier version differs from the reviewed version")
    asset_name, bundle_name = list(ASSETS)
    run(["-m", "sigstore", "verify", "identity", "--bundle", str(root / bundle_name),
         "--cert-identity", IDENTITY, "--cert-oidc-issuer", ISSUER, ASSETS[asset_name]["digest"]])
    # Rehash inputs after verification; a saved success flag never replaces bytes.
    observe_assets(root)
    archive = cas.read_file(root / asset_name, ASSETS[asset_name]["size"])
    binary = extract_binary(archive)
    cas.write_exact(root / "trivy.bin", binary)  # mode 0600, never chmod executable
    if (root / "trivy.bin").stat().st_mode & 0o111:
        raise ScannerAssetError("scanner data unexpectedly has executable permission")
    receipt = {**evidence, "signature_verified": True,
               "certificate_identity": IDENTITY, "certificate_oidc_issuer": ISSUER,
               "verifier": "sigstore-python", "verifier_version": VERIFIER_VERSION,
               "binary_sha256": hashlib.sha256(binary).hexdigest(), "binary_bytes": len(binary),
               "binary_executable_on_host": False}
    cas.write_exact(root / "verified-assets.json", oci.canonical(receipt))
    return receipt
