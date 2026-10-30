"""Content-free live probe for the ordinary application model-provider boundary."""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langchain_core.tools import tool

from app.agents.router import get_llm, get_model_name


@tool(description="Return the supplied non-secret runtime probe nonce.")
def runtime_probe_echo(nonce: str) -> str:
    return nonce


def validate_probe_calls(calls: list[dict], nonce: str) -> None:
    if len(calls) != 1:
        raise RuntimeError("runtime provider did not produce exactly one tool call")
    call = calls[0]
    if call.get("name") != runtime_probe_echo.name:
        raise RuntimeError("runtime provider selected the wrong probe tool")
    if (call.get("args") or {}).get("nonce") != nonce:
        raise RuntimeError("runtime provider changed the probe nonce")


async def main() -> None:
    nonce = "runtime-boundary-probe-v1"
    model = get_model_name("runtime_fast")
    llm = get_llm("runtime_fast", max_tokens=128, temperature=0)
    response = await llm.bind_tools([runtime_probe_echo]).ainvoke(
        "Call runtime_probe_echo exactly once with nonce runtime-boundary-probe-v1."
    )
    calls = list(getattr(response, "tool_calls", []) or [])
    validate_probe_calls(calls, nonce)
    usage = getattr(response, "usage_metadata", None) or {}
    print(json.dumps({
        "ok": True,
        "provider": "gemini",
        "model": model,
        "tool_call_verified": True,
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
    }, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
