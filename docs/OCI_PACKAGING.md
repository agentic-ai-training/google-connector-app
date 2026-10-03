# Validated artifact to immutable image

Status, 3 October 2026: internal assembly and private retrieval are implemented
locally. This is not an activated production builder or a completed deployment.

```text
Four tenant/source-bound validations
  React tests + build; Spring tests + package
                 |
                 v
Encrypted durable passing result + live artifact reference
                 |
Tenant/run/target authorization; active immutable target
                 |
Pinned IAP/SSH fixed gateway
  exact consumed host binding + cleaned job + private CAS
  bounded binary frame; rehash; no arbitrary file path
                 |
Recheck deletion, revocation and expiry
                 |
Operator-owned runtime policy + pinned OCI base blobs
  rehash manifest/config/layers; verify Linux/AMD64 and rootfs
                 |
Fixed non-executing OCI assembler
  React -> Nginx static files + same-origin /api/ proxy
  Spring Boot JAR -> Java 17 startup contract
                 |
Measured layer/config/image-manifest digests + build evidence
                 |
OCI layout archive (LOCAL COMPONENT)
                 |
----- not yet activated/proven below this line -----
Scoped registry publication and exact readback
Trusted build-job persistence + separate protected signer
Compose bound to the two measured image digests + approved PostgreSQL
Cost/capacity/user grant + one-time deployment dispatch
Private runtime + behavioral/security/restart verification
Authorized Google Chat delivery of the exact verified URL
```

## Implemented boundaries

`validation_controller.fetch_build_artifact` reads only an existing authenticated
tenant's unexpired passing result. The requested run, target and frozen source
must match. The remote envelope is reconstructed from immutable claim/target facts,
not accepted from a model. The host verifies its stored consumed-binding hash and
cleaned job before opening the exact private CAS object. A binary response binds
the reference, size and archive hash. The gateway holds its host slot and transfer
alarm while writing the bytes, preventing parallel slow readers from multiplying
staging memory. There is no new public endpoint or listener.

Deletion, expiry and target revocation are rechecked after transfer. Read-only
retrieval can be retried; it does not rerun compilation or re-create an artifact.
A cleaned host job alone is not proof of a passing validation. The central result
must separately be passing, immutable and present.

`runtime_image_store.load_runtime_base` reads a private operator policy outside
the generated workspace. Policy v1 maps the two supported roles to a manifest
digest and protected OCI layout directory. Symlinks, unsafe ownership/permissions,
hardlinked files, unknown roles and changed blobs are rejected. The selected image
index must contain the approved manifest. All compressed layers and their uncompressed
diff IDs are verified with bounded reads; no base archive is extracted onto the host.

`oci_packager.package_validated_artifact` re-inspects actual artifact bytes and their
file-set hash before constructing an image. It never runs generated code, a
Dockerfile, a shell, a build hook, or a network operation. Its role-specific runtime
commands and configuration are trusted code, not model-provided strings. Layer paths,
ownership, permissions, ordering, timestamps and compression are normalized. Whiteout
names are rejected so generated outputs cannot delete lower-layer files. The output
contains actual content-addressed manifest/config/layer blobs, not guessed image IDs.

The frontend role runs Nginx as UID/GID 65532 on port 8080, serves the built React
bundle, and proxies `/api/` to `backend:8080` without stripping the path. The backend
role starts the exact Boot JAR using the approved Temurin Java 17 binary as the same
non-root user. Known incompatible application class headers (including Java 21 and
preview bytecode) are rejected. These structural checks do **not** prove application
behavior or dependency compatibility; actual startup/API/database/browser tests remain
mandatory. Additional languages or runtime versions require separate versioned
contracts, not an implicit weakening of these profiles.

`validation_controller.package_build_artifact` connects authorized retrieval to the
operator-policy loader and real assembler off the event loop, then rechecks private
result and active target state. This function is internal controller code. Its policy
path and source workspace are trusted worker configuration, never request text.
The production worker and Rust broker do not yet call this path.

The fixed artifact-read gateway was installed on the existing VM using the idle-slot,
exact-base-hash two-file installer. Its latest launcher SHA-256 is
`6e76e0b4268530dc50847237da91a2f289be1194b2408db9c458db1f241e44e1`;
the helper remains `1a6e04a0e56f0893335bd9d6476b794e01cd4c563d9dd9e1fe7d4cf9136fdc0d`.
The isolated import check passed and image policy was unchanged. Actual React/Spring
artifact transfer on that host still awaits the real offline toolchains; installation
and local mocked proofs must not be described as a live generated-image build.

## Bounds

| Resource | Bound |
|---|---:|
| Artifact archive | 128 MiB; 4,000 files |
| Base metadata object | 1 MiB |
| Compressed base layers | 512 MiB total; 64 layers |
| Unpacked base data for diff-ID checks | 2 GiB, streamed |
| Runtime policy | 32 KiB; two roles |
| Artifact transfer frame header | 8 KiB |
| Host transfer alarm | 45 seconds |
| Client transfer timeout | 120 seconds |

Assembly still needs a dedicated keyless worker with enforced CPU/memory/storage/time
limits and durable build-job ownership before production use. In-process thread
cancellation does not stop CPU work; it is not equivalent to an isolated worker.
No runtime base has been enrolled on the VM, no image published, no signing key
installed and no generated Service started by this component.

## Remaining integration and proof

Checkpoint verification: 426 coding tests passed with the real loopback PostgreSQL
fixtures; targeted Flake8/Bandit and diff checks passed. The OCI tests use synthetic
runtime/class fixtures, not a running Nginx/Java app. Controller integration exercises
the actual assembler and protected base-store loader, with the remote transfer mocked.
Do not use this count to claim live production deployment or behavior.

1. Provision and scan approved Nginx/Java/PostgreSQL bases and complete offline
   Node/Maven validation toolchains under the existing hosting envelope.
2. Add durable image-build intent/result/storage retention and finite recovery to
   the shared capacity ledger; do not hold database locks over assembly or network I/O.
3. Run the dedicated keyless builder and authenticated publication controller.
   Record exact registry manifest readback; a local image ID or a tag is insufficient.
4. Bind measured image evidence, run/tenant/source/target/validation proof set to a
   protected signer; keep its key outside model and generated-code authority.
5. Wire actual Compose generation, secrets, Service create/start/reconciliation,
   security/behavioral verification and final authorized URL delivery.
6. Demonstrate the complete durable request and fault recovery, not merely mocked
   HTTP or a structurally valid OCI layout.

References: [OCI manifests](https://github.com/opencontainers/image-spec/blob/main/manifest.md),
[OCI image configuration](https://github.com/opencontainers/image-spec/blob/main/config.md),
[OCI layers and whiteouts](https://github.com/opencontainers/image-spec/blob/main/layer.md),
[OCI layouts](https://github.com/opencontainers/image-spec/blob/main/image-layout.md).
