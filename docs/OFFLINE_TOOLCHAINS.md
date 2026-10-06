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
Host-backed packaging storage remains noexec. This source revision is not installed
on the VM. Never substitute native host execution to work around a toolchain failure.

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
Current VM policy still approves only the Python synthetic profile. Protected export,
host enrollment, gVisor proof, real Nginx/Java runtime bases, central financial
admission, registry/signing, app behavior and final Chat delivery remain open.
See [isolated validation](ISOLATED_VALIDATION.md),
[host bundle operations](HOST_BUNDLE_OPERATIONS.md) and [OCI packaging](OCI_PACKAGING.md).
