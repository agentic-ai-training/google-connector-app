#!/usr/bin/env python3
"""Validate or explicitly publish version-controlled dashboards to Grafana."""

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
DASHBOARDS = (
    ROOT / "monitoring/grafana/dashboards/google-connector.json",
    ROOT / "monitoring/grafana/dashboards/session-operations.json",
)
CONFIRMATION = "SYNC GRAFANA DASHBOARDS"


def remap_datasource_uid(value, source_uid: str, target_uid: str):
    """Return a dashboard copy with one portable datasource UID rebound."""
    if isinstance(value, list):
        return [remap_datasource_uid(item, source_uid, target_uid) for item in value]
    if isinstance(value, dict):
        return {
            key: (
                target_uid if key == "uid" and item == source_uid
                else remap_datasource_uid(item, source_uid, target_uid)
            )
            for key, item in value.items()
        }
    return value


def load_local_credentials(path: Path | None = None) -> bool:
    """Load an untracked local env file without replacing explicit process values."""
    target = path or ROOT / ".env.local"
    return load_dotenv(target, override=False)


def build_dashboard_payload(path: Path, folder_uid: str | None = None) -> dict:
    dashboard = json.loads(path.read_text(encoding="utf-8"))
    prometheus_uid = os.getenv("GRAFANA_PROMETHEUS_DATASOURCE_UID", "").strip()
    if prometheus_uid:
        dashboard = remap_datasource_uid(dashboard, "prometheus", prometheus_uid)
    required = ("title", "uid", "panels", "schemaVersion")
    missing = [key for key in required if not dashboard.get(key)]
    if missing:
        raise ValueError(f"{path}: missing required dashboard keys {missing}")
    payload = {"dashboard": dashboard, "overwrite": True,
               "message": "Version-controlled Google Connector dashboard sync"}
    if folder_uid:
        payload["folderUid"] = folder_uid
    return payload


def publish(base_url: str, token: str, payload: dict) -> dict:
    dashboard = dict(payload["dashboard"])
    uid = str(dashboard["uid"])
    lookup = Request(
        f"{base_url.rstrip('/')}/api/dashboards/uid/{quote(uid, safe='')}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urlopen(lookup, timeout=30) as response:  # nosec B310: configured HTTPS endpoint
            existing = json.loads(response.read().decode()).get("dashboard") or {}
            if existing.get("version") is not None:
                dashboard["version"] = int(existing["version"])
    except HTTPError as exc:
        if exc.code != 404:
            detail = exc.read().decode(errors="replace")[:500]
            raise RuntimeError(
                f"Grafana cannot reconcile existing dashboard {uid!r} "
                f"(HTTP {exc.code}): {detail}"
            ) from exc
    payload = {**payload, "dashboard": dashboard}
    request = Request(
        f"{base_url.rstrip('/')}/api/dashboards/db",
        data=json.dumps(payload).encode(), method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=30) as response:  # nosec B310: configured HTTPS endpoint
            return json.loads(response.read().decode())
    except HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:500]
        raise RuntimeError(f"Grafana returned HTTP {exc.code}: {detail}") from exc


def main() -> int:
    load_local_credentials()
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="Publish after validation; otherwise perform a dry run")
    parser.add_argument("--confirmation", default="")
    parser.add_argument("--folder-uid", default=os.getenv("GRAFANA_FOLDER_UID", ""))
    args = parser.parse_args()
    payloads = [build_dashboard_payload(path, args.folder_uid or None)
                for path in DASHBOARDS]
    if not args.apply:
        print(json.dumps({"validated": len(payloads), "published": 0,
                          "dashboards": [item["dashboard"]["uid"] for item in payloads]}))
        return 0
    if args.confirmation != CONFIRMATION:
        raise SystemExit(f"Refusing publication: pass --confirmation '{CONFIRMATION}'")
    base_url = os.getenv("GRAFANA_URL", "").strip()
    token = os.getenv("GRAFANA_SERVICE_ACCOUNT_TOKEN", "").strip()
    if not base_url or not token:
        raise SystemExit("GRAFANA_URL and GRAFANA_SERVICE_ACCOUNT_TOKEN are required")
    if not base_url.startswith("https://"):
        raise SystemExit("GRAFANA_URL must use HTTPS")
    results = [publish(base_url, token, payload) for payload in payloads]
    print(json.dumps({"validated": len(payloads), "published": len(results),
                      "dashboards": [item.get("uid") for item in results]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
