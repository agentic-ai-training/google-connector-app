"""Operator-owned Coolify credential references for trusted deployment adapters.

References are lookup labels, never file paths or environment names supplied by a
model. Values are resolved only at the provider call boundary and never serialized.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import stat
from typing import Literal


class OperatorSecretError(RuntimeError):
    def __init__(self, category: str):
        self.category = category
        super().__init__(f"operator_secret:{category}")


REFERENCE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_/-]{2,127}$")
VARIABLE = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")


@dataclass(frozen=True)
class TokenBinding:
    reference: str
    variable: str
    source: Literal["private_file", "environment"]
    expires_at: datetime
    path: Path | None = None

    def __post_init__(self):
        if (not REFERENCE.fullmatch(self.reference)
                or not VARIABLE.fullmatch(self.variable)
                or self.expires_at.utcoffset() is None
                or (self.source == "private_file") != (self.path is not None)):
            raise ValueError("invalid operator token binding")


class OperatorTokenResolver:
    """Callable compatible with the Coolify adapter's ``resolve_token`` slot."""

    def __init__(self, bindings: tuple[TokenBinding, ...]):
        if not bindings or len({item.reference for item in bindings}) != len(bindings):
            raise ValueError("operator token references must be unique and nonempty")
        self._bindings = {item.reference: item for item in bindings}

    def __call__(self, reference: str) -> str:
        binding = self._bindings.get(reference)
        if binding is None:
            raise OperatorSecretError("reference_not_registered")
        if datetime.now(timezone.utc) >= binding.expires_at.astimezone(timezone.utc):
            raise OperatorSecretError("credential_expired")
        if binding.source == "environment":
            token = os.environ.get(binding.variable)
        else:
            token = _read_private_file(binding.path, binding.variable)
        if (not isinstance(token, str) or not 1 <= len(token) <= 2048
                or not token.isascii() or any(char.isspace() for char in token)):
            raise OperatorSecretError("credential_unavailable")
        return token


def _read_private_file(path: Path, variable: str) -> str:
    if path is None:
        raise OperatorSecretError("credential_unavailable")
    try:
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        file_flags = os.O_RDONLY | os.O_NOFOLLOW
        directory_fd = os.open(path.parent, directory_flags)
        try:
            directory = os.fstat(directory_fd)
            if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.getuid()
                    or stat.S_IMODE(directory.st_mode) & 0o077):
                raise OperatorSecretError("unsafe_credential_file")
            file_fd = os.open(path.name, file_flags, dir_fd=directory_fd)
            try:
                file = os.fstat(file_fd)
                if (not stat.S_ISREG(file.st_mode) or file.st_uid != os.getuid()
                        or stat.S_IMODE(file.st_mode) & 0o077 or file.st_size > 4096):
                    raise OperatorSecretError("unsafe_credential_file")
                content = os.read(file_fd, 4097)
            finally:
                os.close(file_fd)
        finally:
            os.close(directory_fd)
    except OperatorSecretError:
        raise
    except OSError:
        raise OperatorSecretError("credential_unavailable") from None
    if len(content) > 4096:
        raise OperatorSecretError("unsafe_credential_file")
    try:
        lines = content.decode("ascii").splitlines()
    except UnicodeError:
        raise OperatorSecretError("credential_unavailable") from None
    prefix = variable + "="
    if len(lines) != 1 or not lines[0].startswith(prefix):
        raise OperatorSecretError("credential_unavailable")
    token = lines[0][len(prefix):]
    # The existing private bootstrap files use shell-style single quotes. Accept
    # only one exact enclosing pair; never evaluate escapes or interpolate data.
    if token.startswith(("'", '"')):
        quote = token[0]
        if len(token) < 3 or not token.endswith(quote) or quote in token[1:-1]:
            raise OperatorSecretError("credential_unavailable")
        token = token[1:-1]
    return token
