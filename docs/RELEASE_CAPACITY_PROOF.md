# Native release image and constrained-runtime proof

29 October 2026 — mechanism added to the existing no-secret Linux candidate CI.
Actual native results are pending; local contract tests are not cloud fit proof.
The focused capacity/process/fail-fast suite passed 86 tests in 6.22s; targeted
lint/security/whitespace checks passed. This did not run real Docker images locally.
No local multi-image build, cloud service, deployment, migration, billing upgrade,
new runner plan or hosting-term extension was started.

## First native result and verifier correction

At exact `1d31927`, main CI 40915805974 passed all four jobs. Candidate run
40915806058 passed 2,727 tests (seven separately gated skips), audits/evaluations,
migrations and all four actual image builds. The image architecture/source/size
checks ran before its fixture, and the actual API/worker started without an
observed OOM. **The capacity run nevertheless failed**, so there are no accepted
memory measurements or complete capacity report from it.

The new harness compared the API's serialized PostgreSQL NUMERIC `"100.00"` to a
Python integer. The route intentionally uses `json.dumps(default=str)`, so its
complete run was rejected by the harness. Verification now parses finite numeric
values while still requiring exactly 100 technical/functional/user-visible
completion, zero model calls, complete steps and nonempty step outputs. Partial,
NaN, boolean and output-free false successes remain rejected.

The harness then misclassified successful network deletion: Docker's missing-
network response is `network <exact-name> not found`, not just `no such ...`.
A read-only local probe confirmed that format without creating/deleting a
resource. The correction recognizes resource-specific absence naming the exact
object, including token boundaries; a missing daemon/socket or another object's
absence is **not** cleanup proof. Builder initialization also now requires its
two asyncpg pool connections, not Alembic's transient one. New regressions cover
these real boundary cases. A fresh corrected native run remains necessary.
Deletion also uses the inspected immutable Docker ID after ownership verification,
not a reusable name; post-removal absence is checked against that same ID.
The corrected focused suite passed 106 tests, plus targeted lint/security and
whitespace checks; actual corrected native execution remains pending.

## What the job now does

The same standard Ubuntu candidate-validation job builds the real `Dockerfile`,
worker, candidate and builder images, reusing their common Docker layers. Each is
labelled with the exact checked-out PR head. After all existing tests, security,
evaluation and migration gates pass, it invokes:

```bash
python -m scripts.verify_release_capacity --source-commit "$CANDIDATE_HEAD_SHA"
```

This is a native Linux/AMD64 proof, not an ARM/Mac estimate. Every image must have
that architecture, an immutable Docker ID, the exact source label and Docker
uncompressed size below **4,000,000,000 bytes**. That is a conservative local image
metric, not an independently observed Railway upload or its final accounting.

The runtime fixture then creates a uniquely named/labelled **internal** network and
PostgreSQL database container. The database uses a bounded tmpfs, with no named
volume, host filesystem mount or published port. No real database or credential is
used. The API performs schema migration on this new synthetic database; worker and
builder migration commands run only afterward, against the same disposable fixture.
The candidate image retains its no-schema-migration command.

API, worker, candidate and idle builder containers get the observed trial ceilings:

- 1,000,000,000 memory bytes; equal memory/swap limits prohibit swap from masking
  a memory shortfall.
- Two CPUs and 1,000 PIDs.
- No privilege, host mounts, additional networks or public port bindings.

The test independently inspects those settings, cgroup-v2 current/peak memory,
swap/PID limits, OOM counters and writable-layer bytes. A smoke peak above 800 MB
fails, preserving at least 20% measured headroom. Writable-layer usage must remain
below one billion bytes. This is not a promise about future peak usage.

The actual API serves version-bound health. The separate actual worker executes
`What can you do?` through authenticated `/runs` admission and durable completion.
The result must have verified completed steps, 100% functional completion and zero
model calls. Candidate health must identify its exact role/version. Builder proof
is only import/idle initialization plus a role-specific actual database connection;
there are no queued candidate builds or Groq keys. Startup/generation are distinct.

Docker commands use the existing bounded owned-process lifecycle, fixed argv-safe
stream merging and a fresh credential-free environment. No provider/Docker-auth
configuration, proxy or remote-daemon variable is inherited. The candidate job has
no production/callback/provider secrets. The fixture's internal network restricts
external routing; it is not a general generated-code OS sandbox.

## Cleanup and retained evidence

Planned resource names are recorded before each create/run, including lost CLI
acknowledgement paths. Cleanup reads each exact name and ownership label before
removing it, then independently verifies absence. It never prunes Docker, selects
other containers by broad prefix, or touches user databases/volumes. An unavailable
daemon or wrong label cannot be reported as proven cleanup. Exceptions trigger
cleanup. Absence after an unacknowledged create is also uncertain, since the
daemon could still be processing it; that path cannot produce a passed report.
Failed startup records only whitelisted role/running/exit/OOM facts, not arbitrary
logs, container environment or credentials. Abrupt runner/host destruction is not a locally proven graceful-cleanup
path. These are ephemeral CI-only resources, not remote cloud jobs.

Only after successful cleanup does `release-capacity.json` contain source/image
identities, sizes, constrained memory/PID/writable-layer measurements and the
synthetic run result. No body, credential, arbitrary container configuration or
raw log is published. The small artifact is retained seven days; no image is
uploaded as a GitHub artifact. Failure remains failure through the existing Bash
`pipefail` gate. The job has a 35-minute overall deadline.

## Remaining proof and placement decisions

Passing this test would prove native image construction and bounded synthetic
startup/run behavior only. It does **not** establish:

- Ollama model load/embedding memory, Alloy footprint or their placement.
- Live Gemini/Groq/Google latency, quotas, private data, RAG, coding generation or
  large tool-result memory under production workload.
- Total project cost/credits, five-service placement, private networking on Railway,
  provider disk/image accounting, live health or a complete canary lifecycle.
- Cryptographic image/source provenance, adversarial security, sustained load,
  restore drills, GCP funding extension or the full original goal.

Those remain explicit checks. A measured miss requires optimizing the actual
image/service or reviewing placement/capacity—not raising limits, buying a runner,
moving protected budgets, or silently claiming the trial fits.

References: [Docker memory/swap limits](https://docs.docker.com/engine/containers/resource_constraints/),
[internal network option](https://docs.docker.com/reference/cli/docker/network/create/).
