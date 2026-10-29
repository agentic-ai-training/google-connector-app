"""Make the isolated Railway candidate public only when it contains an API surface."""

import json
import os
import subprocess


service = os.environ["RAILWAY_CANDIDATE_WORKER_SERVICE"]
surfaces = set(filter(None, os.environ.get("CANDIDATE_RUNTIME_SURFACES", "").split(",")))


def railway(*args):
    return subprocess.check_output(
        ["railway", *args, "--service", service, "--json"],
        text=True,
    )


raw = json.loads(railway("domain", "list"))
domains = raw if isinstance(raw, list) else raw.get("domains", raw.get("data", []))
if "api" in surfaces and not domains:
    created = json.loads(railway("domain", "--port", "8000"))
    domains = [created]
elif "api" not in surfaces:
    for item in domains:
        identifier = item.get("id") or item.get("domain") or item.get("url")
        if identifier:
            subprocess.check_call([
                "railway", "domain", "delete",
                str(identifier), "--yes", "--service", service,
            ])
    domains = []

domain = None
if domains:
    item = domains[0]
    domain = item.get("domain") or item.get("url")
    if domain and not domain.startswith("http"):
        domain = "https://" + domain

with open("candidate-domain.json", "w", encoding="utf-8") as handle:
    json.dump({"deployment_url": domain, "runtime_surfaces": sorted(surfaces)}, handle)
print(json.dumps({"deployment_url": domain, "runtime_surfaces": sorted(surfaces)}))
