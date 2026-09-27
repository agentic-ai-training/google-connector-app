"""Bounded, credential-isolated Coolify inventory transport for trusted controllers.

No model-supplied URLs, arbitrary endpoints, shell commands, or deployment writes.
Read evidence is not deployment approval or functional verification.
"""
from __future__ import annotations

import json
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator


class CoolifyTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,63}$")
    base_url: str
    team_id: int = Field(ge=0, strict=True)
    # This reference identifies an operator-owned secret, never its plaintext value.
    inventory_secret_ref: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_/-]{2,127}$")

    @field_validator("base_url")
    @classmethod
    def validate_origin(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (not parsed.hostname or parsed.username or parsed.password or parsed.query
                or parsed.fragment or parsed.path not in {"", "/"}):
            raise ValueError("Coolify must use an operator-approved origin, not an arbitrary URL")
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1"}
        ):
            raise ValueError("HTTPS is required except for a local SSH/IAP tunnel")
        # Force malformed port values to fail validation now, before credential lookup.
        _ = parsed.port
        return value.rstrip("/")


class CoolifyAPIError(RuntimeError):
    """Stable sanitized category; provider bodies and bearer headers are never included."""

    def __init__(self, category: str, status: int | None = None):
        self.category = category
        self.status = status
        super().__init__(f"coolify:{category}" + (f":http_{status}" if status else ""))


class CoolifyInventory:
    MAX_BYTES = 1_048_576

    def __init__(self, target: CoolifyTarget, token: str, *, transport=None):
        if not token or any(c.isspace() for c in token):
            raise ValueError("invalid inventory credential")
        self.target = target
        self._client = httpx.AsyncClient(
            base_url=target.base_url, headers={"Authorization": f"Bearer {token}",
                                              "Accept-Encoding": "identity"},
            timeout=httpx.Timeout(15, connect=5), follow_redirects=False, trust_env=False,
            transport=transport,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self._client.aclose()

    async def _get(self, path: str):
        try:
            async with self._client.stream("GET", path) as response:
                if response.status_code != 200:
                    category = {
                        401: "authentication", 403: "permission", 404: "not_found",
                        429: "rate_limit",
                    }.get(response.status_code, "provider_error")
                    raise CoolifyAPIError(category, response.status_code)
                if response.headers.get("content-encoding", "identity") != "identity":
                    raise CoolifyAPIError("unsupported_compression")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > self.MAX_BYTES:
                        raise CoolifyAPIError("response_too_large")
                    body.extend(chunk)
                try:
                    return json.loads(body)
                except (ValueError, UnicodeError, RecursionError):
                    raise CoolifyAPIError("invalid_json") from None
        except httpx.TimeoutException:
            raise CoolifyAPIError("timeout") from None
        except httpx.HTTPError:
            raise CoolifyAPIError("transport") from None

    async def verify_team(self) -> dict:
        value = await self._get("/api/v1/teams/current")
        if (not isinstance(value, dict) or type(value.get("id")) is not int
                or value["id"] != self.target.team_id):
            raise CoolifyAPIError("team_mismatch")
        return {"team_id": value["id"], "target_id": self.target.target_id}

    async def servers(self) -> list[dict]:
        # Verify actual token scope before accepting any inventory as target evidence.
        await self.verify_team()
        values = await self._get("/api/v1/servers")
        if not isinstance(values, list) or len(values) > 1000:
            raise CoolifyAPIError("invalid_inventory")
        result = []
        for value in values:
            if (not isinstance(value, dict) or not isinstance(value.get("uuid"), str)
                    or not 1 <= len(value["uuid"]) <= 128):
                raise CoolifyAPIError("invalid_inventory")
            # Never propagate full server/settings payloads, which may contain secrets.
            settings = value.get("settings") or {}
            if not isinstance(settings, dict):
                raise CoolifyAPIError("invalid_inventory")
            result.append({
                "uuid": value["uuid"],
                "reachable": settings.get("is_reachable") is True,
                "usable": settings.get("is_usable") is True,
            })
        return result
