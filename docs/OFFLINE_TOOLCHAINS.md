# Reviewed offline React and Spring toolchains

## Authority and workflow

These are operator-owned dependency-acquisition recipes, not generated Dockerfiles
or model-callable build commands. Only fixed public dependency specifications and
the two fixed public Spring seed files enter the bounded build contexts. No customer
repository, OAuth/provider key or production database enters acquisition. Generated
source runs later in a separate keyless, network-disabled sandbox. Operator Docker
credential helpers may authenticate a public-image pull; credentials are never copied
into either image.

```text
review public specs + immutable AMD64 bases + exact build contexts
                       |
             operator dependency acquisition
                       |
        fixed positive and deliberate negative tests
                       |
     local Docker proof (NOT Linux/gVisor enforcement proof)
                       |
  protected image export + host enrollment/read-back [pending]
                       |
        live Linux/gVisor four-profile proof [pending]
                       |
       funded central dispatch and image approval [pending]
                       |
   generated source -> tests/build -> verified packaging
                       |
  signing/deployment/application behavior/Chat proof [pending]
```

The pure bounded inspector in `app/coding/toolchain_spec.py` is not image approval
authority. It rejects unreviewed direct versions, npm lifecycle hooks, duplicate JSON
keys, private/credentialed URLs, missing SHA-512 integrity, links and traversal. The
Maven catalog rejects entities, alternate repositories, executable plugin configuration,
extra dependencies, duplicated fields and extra build authority. Dependency changes
require a new reviewed recipe and fresh offline proof. This is not an arbitrary-POM
execution service.

## Fixed catalogs

| Role | Reviewed dependencies |
| --- | --- |
| React | Node 24 AMD64 base; React/react-dom 19.3.0; Vite 8.3.2; Vitest 5.0.3; plugin-react 6.1.2 |
| Spring | Maven/JDK 17 AMD64 base; Spring Boot 4.1.1; MVC/security/JPA; PostgreSQL driver; H2/test/security-test; dependency plugin 3.11.0 |

The complete React transitive lock is checked in. npm acquisition and offline
installation both disable lifecycle scripts. Native packages must already work from
the cache; enabling hooks inside validation is not a fallback.

Maven `go-offline` alone did not fetch the dynamically selected
`surefire-junit-platform:3.5.6` provider. Acquisition now also tests/packages a fixed
public BCrypt/JUnit fixture and Boot main class. Seed source and build outputs are
removed from the final image. The context allows those two exact Java files, not every
file in the seed directory. Tests assert equality with the separate public validation
fixtures. There is no online fallback during generated-code validation.
See [npm ci](https://docs.npmjs.com/cli/commands/npm-ci/) and
[Maven go-offline](https://maven.apache.org/plugins/maven-dependency-plugin/go-offline-mojo).

## Local operator commands

Run from the repository root with Docker Desktop running. AMD64 emulation on this
Mac matches the target instruction set, but is not Linux/gVisor enforcement proof.

```sh
docker build --platform linux/amd64 --tag gca-reviewed-react:offline-v1 \
  --label org.gca.toolchain.operator-only=true toolchains/react
docker build --platform linux/amd64 --tag gca-reviewed-spring:offline-v1 \
  --label org.gca.toolchain.operator-only=true toolchains/spring

# Default: local Unix Docker target and image metadata inspection only.
.venv/bin/python -m scripts.probe_local_offline_toolchains --role react
.venv/bin/python -m scripts.probe_local_offline_toolchains --role spring

# Execute only the checked-in nonsensitive fixture.
.venv/bin/python -m scripts.probe_local_offline_toolchains --role react --run
.venv/bin/python -m scripts.probe_local_offline_toolchains --role react --run --negative
.venv/bin/python -m scripts.probe_local_offline_toolchains --role spring --run
.venv/bin/python -m scripts.probe_local_offline_toolchains --role spring --run --negative
```

The probe resolves the local tag to an immutable image ID before creation. It rejects
remote Docker contexts, unexpected platform/user, volumes, entrypoints, healthchecks
and role/base labels. Workloads have network none, read-only root, UID/GID 65532,
zero capabilities, no-new-privileges, one CPU, 1 GiB/no extra swap, 128 PIDs and a
finite lifetime. Cleanup checks the exact name/image/fixture hash and deletes only
that container. Unknown identities are not removed. A lost observation cannot prove
cleanup; this fixed local helper does not replace the production host journal.

Actual tests exposed Docker's implicit tmpfs noexec: Vitest could not execute its
workspace binary, and Jansi could not load a transient native library from `/tmp`.
The launcher source now permits execution on only the bounded sandbox-internal
workspace and tmp mounts, retaining nosuid/nodev and all other restrictions.
Host-backed packaging storage remains noexec. The reviewed source revision was
subsequently installed/read-back verified on the existing VM on 6 October; see the
separate host evidence below. Never substitute native host execution to work around
a toolchain failure.

## Measured local evidence — 6 October 2026

| Role | Docker Desktop image ID |
| --- | --- |
| React | `sha256:d3d2984e4d4b8e90a5b93b0f3a89591c8958fd42a8d8f68de4ac5eafaeeba9de` |
| Spring | `sha256:1f90c0fe3495fc3cc00f8b3a9cc6681782238fa1f1dd8a57c8583bac31c68943` |

React passed two Vitest tests and Vite build with network none, checking nonempty
dist/index.html. Spring passed offline test/package, produced a nonempty Boot JAR,
and its bounded Surefire XML proved an executed passing test. Both deliberately
failing mutations returned nonzero with the exact fixed assertion marker. Independent
cleanup found no remaining gca-local-toolchain containers. These are real executions,
not simulated test results. Source fingerprints:

- React positive: `16b1eca2c9b57ecafda6f4bc33cc9b6eab48e1bf055b34c851dbb90d912f21f0`.
- React negative: `1ae8ec620c9b3e98844f7c705701f9335e97271341102f7e24a05ac2b69edd57`.
- Spring positive: `6f6d5541ac1409745589933173b1b890b648b916544f312fb99c979c90aa45a8`.
- Spring negative: `b65e64b88b33a497766387d5a20c8a83bd6ac2a95e640a5ae95dde0fcb3e946f`.

Verification: 804 coding tests passed against real loopback PostgreSQL, no skips;
focused Flake8 and Bandit passed (Bandit reported existing unused nosec comments).
One google-genai dependency deprecation warning remains in the user-owned Vertex test.
This covers the coding subsystem, not the complete original project acceptance audit.

There were zero model calls, private-source inputs or new cloud resources. Nothing
was deployed or approved for production. Do not interchange image index, platform
manifest and config digests; read back each host's actual identity after archive load.
The seven-file host bundle was independently rehashed after installing this launcher
correction: installation `fe1010fdde74f46221cd937f56b3f084bee4206978f6d294c6bf5c452df8e2bc`,
snapshot `835b2bd12a13d4249bb210c159ee5ddf441b3f60c34654e4059dcef77083ee2f`.
The bound synthetic Python test passed, rejecting changed source, unbound dispatch
and replay; cleanup passed and lost result was not inferred as test success. The
final independent projected health check verified all six Coolify containers healthy,
an idle journal and zero scoped sandbox containers. The initial broad health query
did not establish health; its outcome was not used as positive evidence. Only an
explicitly inspected obsolete code rollback copy was retired;
journals/artifacts/data and the latest predecessor were preserved.

Current VM policy still approves only the Python synthetic profile. Protected export,
host enrollment, gVisor proof, real Nginx/Java runtime bases, central financial
admission, registry/signing, app behavior and final Chat delivery remain open.
See [isolated validation](ISOLATED_VALIDATION.md),
[host bundle operations](HOST_BUNDLE_OPERATIONS.md) and [OCI packaging](OCI_PACKAGING.md).

## Streaming custody inspection — 6 October 2026

The Mac's free space fell below the combined archive size. The fixed operator
scanner avoids saving a large archive file locally:

```sh
.venv/bin/python -m scripts.probe_toolchain_archive --role react
.venv/bin/python -m scripts.probe_toolchain_archive --role spring
```

It uses the same clean-environment local Unix Docker context as the fixture probe,
resolves the role tag to its immutable ID, then exports **by that ID**, without
tags. It never calls image load, extraction, execution, cloud transport or approval.
Docker's daemon may still use its own working storage; this is not a zero-disk-I/O
claim. The source-only scanner is not installed on the VM or exposed to models.

The archive inspector validates classic tar framing before tarfile can allocate
PAX/GNU extended-header bodies. It rejects links/devices/extensions, traversal,
duplicates, unknown files, extra blobs, malformed descriptors, tag overrides,
privileged/runtime-role mismatch and nonzero trailing data. It hashes every blob,
requires full reachable graph equality, distinguishes index/platform/config digests
and hashes the complete stream including padding. Limits: 512 MiB input, 128 entries,
16 MiB retained small metadata and 1 MiB read chunks. The library deadline is checked
between reads; the dedicated Unix CLI adds a 150-second blocking-read watchdog and
reaps only its own exporter process group. Transport still needs its own deadline.

| Role | Observed archive bytes | SHA-256 |
| --- | ---: | --- |
| React | 108,749,312 | `bbe2dde5593ef0fff2d589f8fe9de801aa02d601ddc66992d3cf2a3550eeed07` |
| Spring | 509,887,488 | `14f01f1dfcbe010f885012f01b56c583552935304310c9a31a35928462706595` |

Repeated scans of each current image produced the same exact archive digest; this
is observed stability for these exports, not a guarantee for every Docker release.
Node platform/config digests are `3ea6582e...` / `21dc647e...`; Spring's are
`b0eb5e11...` / `3269eb8c...`. They differ from the image index IDs listed above.
Full receipts are emitted by the command. None is interchangeable with the archive
byte hash or sufficient by itself to authorize a host policy change.

These are custody checks, not independent build provenance, approved base origin,
live gVisor execution, funded dispatch or deployment proof. The CLI's local tag and
labels do not grant production authority. The eventual operator enrollment must
bind reviewed recipe/provenance and exact immutable receipt, import through protected
state with crash recovery, read back the loaded identity, and complete real host
positive/negative tests before changing policy. Those stages remain open. Low local
disk space and current October billing reconciliation still need resolution.

Regression evidence for this source checkpoint: 834 coding tests passed against
real loopback PostgreSQL, without skips, before the final two end-marker cases were
added. Those cases and the final focused archive/probe suite were separately run.
Focused Flake8/Bandit and diff checks passed. This is not trusted CI or production
execution evidence; the single existing google-genai deprecation warning remains.
