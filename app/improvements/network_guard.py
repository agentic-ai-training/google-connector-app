"""Process-local outbound hostname allowlist for the credential-free builder job."""

from contextlib import contextmanager
import socket


@contextmanager
def allowlisted_dns(hostnames: set[str]):
    """Deny resolution of hosts outside the exact normalized allowlist."""
    allowed = {value.casefold().rstrip(".") for value in hostnames if value}
    original = socket.getaddrinfo

    def guarded(host, *args, **kwargs):
        value = host.decode("ascii") if isinstance(host, bytes) else str(host)
        normalized = value.casefold().rstrip(".")
        if normalized not in allowed:
            raise PermissionError(f"Candidate builder outbound host is forbidden: {normalized}")
        return original(host, *args, **kwargs)

    socket.getaddrinfo = guarded
    try:
        yield
    finally:
        socket.getaddrinfo = original
