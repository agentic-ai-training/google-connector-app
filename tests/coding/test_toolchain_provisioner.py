import ast

from scripts.provision_validation_python_toolchain import (
    CONFIG_DIGEST, IMAGE_ID, REMOTE_PROVISIONER, REPOSITORY_DIGEST, maintenance_failure,
)


def test_fixed_maintenance_failure_does_not_leak_provider_output():
    output = b"private credential=value\nRuntimeError: validation slot is unreconciled\n"
    assert maintenance_failure(output) == "validation slot is unreconciled"
    assert maintenance_failure(b"RuntimeError: private credential=value") == (
        "operator state or transport requires inspection"
    )


def test_remote_synthetic_source_contains_newlines_not_literal_escape_sequences():
    writes = [node.args[0].value for node in ast.walk(ast.parse(REMOTE_PROVISIONER))
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and node.func.attr == "write_text"]
    assert writes == ["answer=42\n", "def invalid(:\n"]


def test_containerd_pin_is_the_exact_platform_manifest_not_config_blob():
    assert IMAGE_ID == REPOSITORY_DIGEST.split('@')[1]
    assert IMAGE_ID != CONFIG_DIGEST
    assert "(request['image_id'],'amd64','linux')" in REMOTE_PROVISIONER
