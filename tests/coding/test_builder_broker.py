from app.improvements.builder_tools import BoundedRepositoryTools


class StubRuntime:
    def __init__(self, responses):
        self.responses = responses
        self.requests = []

    def invoke(self, request):
        self.requests.append(request)
        return self.responses[request["tool"]]


def test_builder_routes_generic_repository_reads_through_shared_broker(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("first\nsecond\n")
    runtime = StubRuntime({
        "inventory": {"ok": True, "result": {
            "files": ["app/main.py"], "truncated": False,
        }},
        "search_literal": {"ok": True, "result": {
            "matches": [{"path": "app/main.py", "line": 2, "excerpt": "second"}],
            "truncated": False,
        }},
        "read_lines": {"ok": True, "result": {"content": "second"}},
    })
    tools = BoundedRepositoryTools(tmp_path)
    tools.rust_runtime = runtime

    assert tools.list_files("app")["broker"] == "rust-v0.1"
    assert tools.search("second", ["app"])["broker"] == "rust-v0.1"
    assert tools.read("app/main.py", 2, 2)["content"] == "second"
    assert [request["tool"] for request in runtime.requests] == [
        "inventory", "search_literal", "read_lines",
    ]


def test_builder_does_not_mask_shared_broker_denial(tmp_path):
    (tmp_path / "app").mkdir()
    runtime = StubRuntime({
        "inventory": {
            "ok": False,
            "error": {"code": "sensitive_path", "message": "denied"},
        },
    })
    tools = BoundedRepositoryTools(tmp_path)
    tools.rust_runtime = runtime

    try:
        tools.list_files("app")
    except ValueError as exc:
        assert "sensitive_path" in str(exc)
    else:
        raise AssertionError("broker denial must fail closed")
