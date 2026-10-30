from datetime import datetime, timedelta, timezone

import pytest

from app.coding.operator_secrets import (
    OperatorSecretError, OperatorTokenResolver, TokenBinding,
)


def _binding(path, *, expires_at=None):
    return TokenBinding(
        reference="coolify/pilot/inventory", variable="COOLIFY_INVENTORY_TOKEN",
        source="private_file", path=path,
        expires_at=expires_at or datetime.now(timezone.utc) + timedelta(hours=1),
    )


def _private_file(tmp_path, value="COOLIFY_INVENTORY_TOKEN=test-token"):
    directory = tmp_path / "tokens"
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    path = directory / "inventory.env"
    path.write_text(value)
    path.chmod(0o600)
    return path


def test_registered_private_token_is_resolved_without_serializing_value(tmp_path):
    path = _private_file(tmp_path)
    resolver = OperatorTokenResolver((_binding(path),))
    assert resolver("coolify/pilot/inventory") == "test-token"
    with pytest.raises(OperatorSecretError, match="reference_not_registered"):
        resolver("coolify/pilot/other")
    assert "test-token" not in repr(resolver)


def test_private_bootstrap_single_quoted_token_is_parsed_without_evaluation(tmp_path):
    path = _private_file(tmp_path, "COOLIFY_INVENTORY_TOKEN='test-token'\n")
    resolver = OperatorTokenResolver((_binding(path),))
    assert resolver("coolify/pilot/inventory") == "test-token"


@pytest.mark.parametrize("fault", ["group_file", "group_dir", "symlink", "multiline", "wrong_key"])
def test_private_token_fails_closed_on_unsafe_file(tmp_path, fault):
    path = _private_file(tmp_path)
    if fault == "group_file":
        path.chmod(0o640)
    elif fault == "group_dir":
        path.parent.chmod(0o750)
    elif fault == "symlink":
        link = path.parent / "linked.env"
        link.symlink_to(path)
        path = link
    elif fault == "multiline":
        path.write_text("COOLIFY_INVENTORY_TOKEN=test-token\nEXTRA=bad\n")
    else:
        path.write_text("OTHER_TOKEN=test-token\n")
    with pytest.raises(OperatorSecretError):
        OperatorTokenResolver((_binding(path),))("coolify/pilot/inventory")


def test_expiry_precedes_file_read_and_environment_fallback(tmp_path, monkeypatch):
    path = tmp_path / "missing.env"
    binding = _binding(path, expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    with pytest.raises(OperatorSecretError, match="credential_expired"):
        OperatorTokenResolver((binding,))(binding.reference)
    monkeypatch.setenv("COOLIFY_INVENTORY_TOKEN", "env-token")
    environment = TokenBinding(
        reference="coolify/pilot/inventory", variable="COOLIFY_INVENTORY_TOKEN",
        source="environment", expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    assert OperatorTokenResolver((environment,))(environment.reference) == "env-token"


def test_bad_bindings_are_rejected(tmp_path):
    path = _private_file(tmp_path)
    binding = _binding(path)
    with pytest.raises(ValueError):
        OperatorTokenResolver((binding, binding))
    with pytest.raises(ValueError):
        TokenBinding(
            reference="../wrong", variable="COOLIFY_INVENTORY_TOKEN",
            source="private_file", path=path,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
    with pytest.raises(ValueError):
        TokenBinding(
            reference="coolify/pilot/inventory", variable="COOLIFY_INVENTORY_TOKEN",
            source="environment", path=path,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )


def test_token_value_with_whitespace_is_rejected(tmp_path):
    path = _private_file(tmp_path, "COOLIFY_INVENTORY_TOKEN=bad token\n")
    with pytest.raises(OperatorSecretError, match="credential_unavailable"):
        OperatorTokenResolver((_binding(path),))("coolify/pilot/inventory")
