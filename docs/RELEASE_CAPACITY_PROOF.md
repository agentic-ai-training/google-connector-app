# Native release image and constrained-runtime proof

Latest accepted backend result: the four Python-role experiment below passed.
The separate Alloy/Ollama experiment is implemented but its native result is
still pending; neither result is permission to deploy or spend protected funds.

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

## Second native result and page-accounting correction

At `cad63a7`, main CI 40918113090 passed all four jobs. Candidate run 40918113076
passed 2,747 tests/seven separately gated skips and reached all five running
fixture containers, with no reported container OOM. The deterministic request and
corrected cleanup completed. It failed the combined cgroup assertion before a
successful capacity artifact; the failed run did not retain individual metric
values, so a separate genuine headroom miss is not yet ruled out.

An actual small Linux-VM probe, using an already cached public image, no network,
read-only root and no database startup, established page size 4,096 and kernel
`memory.max=999997440` for Docker's unchanged requested 1,000,000,000 bytes. Its
temporary container's absence was verified. The kernel's
[page-counter parser](https://github.com/torvalds/linux/blob/master/mm/page_counter.c)
converts bytes to whole pages. Exact byte-equality in the harness was incorrect.
It now requires the downward page-aligned limit for the observed valid page size;
the Docker limit remains exactly one billion bytes, swap/OOM/PID rules remain,
and the 20% margin is calculated from the actual aligned limit. No extra memory
or weaker headroom is accepted. Future failures retain only whitelisted numeric
measurements and role identifiers, not environment/log/body contents. Probe
Python uses isolated imports; actual application code still runs normally.
A fresh native run is required to establish all actual measurements and cleanup.
The updated focused suite passed 116 tests plus targeted lint/security/whitespace
checks. It rejects extra memory/swap, incorrect rounding, invalid page size,
insufficient headroom, OOMs and wrong PID limits; those acceptance rules were not
relaxed to mask a real capacity shortfall.

## Verified native result — 30 October 2026

Exact code `d937ba3216ffcd9c38c337ed3e97b31cb9e3ad72` passed all four jobs in
[main CI 40998185737](https://github.com/agentic-ai-training/google-connector-app/actions/runs/40998185737)
and [candidate validation 40998185739](https://github.com/agentic-ai-training/google-connector-app/actions/runs/40998185739).
The candidate backend passed **2,757 tests**, seven separately gated Temporal-server
skips and one existing warning before the actual image/runtime fixture succeeded.
No failed earlier run has been relabelled as passed.

The downloaded capacity artifact was parsed as inert JSON and independently
checked against the exact source, image IDs/architecture/size, cgroup rules,
zero-model durable completion and successful owned cleanup. A sanitized durable
copy is [retained here](evidence/release-capacity-d937ba3.json); its SHA-256 is
`37185756f0d0931baa7b5a6f45cc9b0315cdf5e51ba20ad6a4bda729516a075a`.
This is the committed JSON's digest, not a cryptographic signature or artifact-ZIP
digest. Source-bound CI observation and the stated test scope remain necessary.

| Role | Image bytes (uncompressed) | Measured peak bytes | Writable-layer bytes |
| --- | ---: | ---: | ---: |
| API | 864,291,138 | 137,342,976 | 3,199,070 |
| Worker | 864,291,138 | 107,081,728 | 2,425,071 |
| Candidate | 858,294,330 | 135,958,528 | 2,974,117 |
| Builder, idle initialization only | 858,294,330 | 54,542,336 | 1,210,073 |

All four images are Linux/AMD64. Docker's requested ceiling remained one billion
bytes; each observed `memory.max` was 999,997,440 bytes at 4,096-byte pages. Swap
max and OOM/kill counters were zero, PID maximum 1,000, and the fixed CPU/port/
mount/network constraints were inspected. The authenticated informational task
completed through the actual API/worker with 100% verified completion and no model
calls. API/candidate version health and builder's two DB pool connections passed.
Only the fixture's owned containers/network were removed, with absence verified.

This closes the **native synthetic image/startup/deterministic-request experiment**
at that exact source. It does not establish real Railway deployment, sustained
load, live model/tool/RAG operation, coding generation, Ollama/Alloy placement,
credits or the whole original goal. The remaining-proof section below still applies.

## What the job now does

### Separate sidecar experiment (30 October; native execution pending)

The existing `CI` workflow has an opt-in `sidecar_capacity` boolean for
`workflow_dispatch`. Only that experiment runs when selected; ordinary PR/push
validation retains all four required jobs. A manual capacity run is not trusted
as a substitute for a candidate's pull-request CI evidence.

```bash
gh workflow run ci.yml --ref agent/non-groq-runtime-provider -f sidecar_capacity=true
```

The standard Linux runner builds source-labelled images and invokes
`python -m scripts.verify_sidecar_capacity --source-commit "$GITHUB_SHA"`.
No larger paid runner or cloud resource is selected. The job has read-only
repository permission, no checked-out Git credential, no provider secrets and a
25-minute deadline. It retains only a small seven-day JSON evidence artifact,
after owned cleanup succeeds. Build-time public downloads are distinct from the
runtime fixture's internal-only network.

- Alloy remains **v1.16.1**, now pinned to its multiarchitecture index digest.
  The exact current `monitoring/alloy/config.alloy` scrapes both synthetic API/
  worker endpoints. A disposable authenticated remote-write sink decodes bounded
  Snappy/protobuf records and verifies all three permitted metric families,
  both jobs, the application label and removal of all six private-ID labels and
  their deliberately planted synthetic values. No real Grafana credential or
  external telemetry destination is used. This measures metrics only, not OTLP
  trace/log delivery.
- `Dockerfile.ollama-capacity` is a **separate CPU-only candidate**, not an edit
  to the existing `docker-compose.yml` Ollama service. The official v0.40.13
  AMD64 release has a fixed SHA-256 and size. A bounded, exclusive public fetch
  verifies it; streaming extraction selects only `bin/ollama` and the CPU root
  libraries, rejects traversal/devices/duplicate paths/unsafe links, and omits GPU
  subdirectories. No fetched shell installer is executed.
- The exact existing `nomic-embed-text` manifest is pinned to
  `0a109f422b47e3a30ba2b10eca18548e944e8a23073ee3f3e947efcf3c45e59f`;
  every public blob's hash and byte count is verified. The model is baked into
  the candidate, so isolated runtime needs neither internet nor a model volume.
  Cloud inference is disabled, parallelism is one and only one model may load.
- A real `/api/embed` probe requires 768 finite, nonzero dimensions. It then
  tests the **current application batch envelope: eight distinct 6,000-character
  inputs, with API truncation disabled**, not a smaller hello-world substitute.
  This is a synthetic resource probe, not a semantic-quality/cross-runtime
  compatibility test or proof of all languages/documents/long-context cases.
- Both services retain the unchanged 1,000,000,000-byte RAM/no-swap/2-CPU/1,000-PID
  constraints, image-size and writable-layer limits, zero-OOM checks and 20%
  measured smoke margin. The fixture reuses exact ownership/immutable-ID cleanup
  from the backend experiment. It publishes no ports and mounts no host data.

The local focused suite passed **143 tests**, with targeted flake8/Bandit and
whitespace checks. These contract tests do not prove native image build, actual
Snappy delivery or Ollama batch fit. A real native failure must be diagnosed;
limits, input size and headroom must not be quietly weakened to obtain green CI.
Production placement, a measured cost/credit lifetime, embedding compatibility,
rollout authorization and all other original requirements remain open.

### Backend experiment

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
