# Pinned scanner databases: real private staging, not a vulnerability scan

Checkpoint **13 October 2026**. Both public data artifacts were acquired on the
existing Delhi VM and independently rehashed afterwards. No scanner opened them,
no OCI base scan completed, no runtime approval changed, and no application was
deployed. Production dependencies, VM shape/disk, STOP and budgets were unchanged.

## Upstream contract and exact snapshot

[Trivy's database documentation](https://trivy.dev/docs/latest/configuration/db/)
distinguishes the vulnerability information database from the Java artifact
identification database. They are OCI **data artifacts**, not runnable container
images. Java coverage must not be silently discarded because its data download
is larger. The public Docker Hub locations below are documented upstream; tags
were used only for discovery. Actual acquisition uses source-reviewed digests.

| Role | Repository / hint | Manifest digest | Compressed archive |
| --- | --- | --- | ---: |
| Vulnerability | `aquasec/trivy-db:2` | `sha256:2f2c5cf8bd240e46942462776aa912feeec723cdf50d224f6296278ac6e3fd10` | 133,148,923 bytes |
| Java identification | `aquasec/trivy-java-db:1` | `sha256:c670fad2358af7bd2ebeabe1ff9d58cadad9c59fcfd9033729a76894b62e7fab` | 982,774,609 bytes |

The GHCR mirror was also inspected read-only. Its manifests have different
creation-annotation bytes/digests, but both referenced the same respective archive
digests. That observation is not a new signature or authority. `publisher_signature_verified`
remains **false** for these databases: source is the reviewed public registry/TLS
and pinned graph, not a claimed signature that was never checked.

Catalog SHA-256:
`79734a08f6dccc4623e6ef7d7bc523a05d4071d57370bde7a5f2cfd4c5086fd1`.
Private host root `/var/lib/gca-scanner-databases-v1/<catalog-sha>/` contains the
two role directories, source-bound plan and immutable receipts. Total staged
regular-file bytes after acquisition/readback: **4,276,265,923** (about 3.98 GiB).

| Object | Size | SHA-256 |
| --- | ---: | --- |
| Vulnerability archive | 133,148,923 | `7f3ba3d6e00f030a48ae163bcdbba68ddade03910ced826cc03400395b6362cc` |
| Expanded `trivy.db` | 1,607,704,576 | `217ebd26bb6d7a72aa42a1c2daed8f512148a106855e4add884b878bbc1bd9c2` |
| Vulnerability metadata | bounded JSON | `2e9a80ca1f354478d8956c6ced231194f8f134e53735a2ff5f097b7bfe1df0b0` |
| Java archive | 982,774,609 | `92af64e106bbab0fa6edbd6a7e8af70a5b60002c6eff4a338eaa5f23c5a1ab38` |
| Expanded `trivy-java.db` | 1,552,633,856 | `8718fff42046c2485b31440313fd49fa487f81a03d339a7655f670f6447153f5` |
| Java metadata | bounded JSON | `84aa81e5b46749162c15d18526d31e7cb9e7a54b8ec430bc930e6b647c16e8a4` |

The expanded file sizes were initially estimated from bounded header-only reads;
that was planning evidence, not whole-blob proof. The subsequent acquisition
verified full compressed hashes, streamed every exact regular member and rehashed
the source archive before/after parsing. The later independent readback rehashed
actual manifests, archives, expanded databases, metadata and receipts.

## Real freshness and expiry

Raw metadata was retained, including its nanosecond-format timestamp strings;
`DownloadedAt: 0001-01-01T00:00:00Z` is publisher data, not our download time.
We do not overwrite it to make an old database appear current.

- Vulnerability schema **2**: updated 13 October **01:09:18.36500669 UTC**;
  publisher `NextUpdate` is 14 October **01:09:18.36500625 UTC**.
- Java schema **1**: updated 13 October **01:09:47.998143143 UTC**;
  publisher `NextUpdate` is 16 October **01:09:47.998142983 UTC**.
- Independent freshness/hash observations: 13 October **05:27:19–05:27:29 UTC**.

The project gate requires age at most **24 hours**, no more than five minutes
future skew, unexpired `NextUpdate`, valid timezone/schema and a bounded update
interval. Thus Java also stops qualifying after 14 October around 01:09:48 UTC,
despite its later publisher `NextUpdate`. Every future scan must recheck freshness;
a saved receipt or today's documentation does not keep data fresh indefinitely.

## Actual resource and custody guards

The fixed operator controller revalidates the pinned IAP target, exact fourteen-
file installed source snapshot, policy/registry, catalog, STOP and idle shared
journal. It uses the existing **one CPU / 3 GiB / no-swap / 128-task** aggregate,
a 270-second alarm, and requires at least 12 GiB existing free disk headroom.
The complete scanner-database store is capped at 8 GiB, 32 regular files and 16
directories. This consumes existing disk capacity; it creates no GCP disk/VM.

Compressed data is streamed in 1-MiB chunks. Partial continuation requires an
exact HTTP 206/Content-Range, then the complete pinned digest. A completed partial
can reconcile without a new download. Expanded partial prefixes are compared to
the verified archive before appending. Complete/partial conflicts, changed bytes,
symlinks, hard links, public/executable inodes and oversized files are rejected.

Only two fixed short regular filenames are allowed per archive. A minimal header
reader rejects PAX/GNU extensions before consuming their declared bodies, rather
than letting a general tar parser allocate an oversized extension first. Expansion
and metadata have independent bounds; no archive path is blindly extracted. Data
does not run as Python, shell, database SQL or a container image.

## Retained source and reproducible observation

Controller SHA-256:
`b7726790ee87433b7af1093488a58ad80e588b8639fe722f0ca4f2e051dffe3f`.
Retained Git blob: `764be61449e1c982e78e59de80c6982d95470232`, under
`refs/gca/scanner-databases/<controller-sha>`.
Installed host source remained
`b84b3c9da1c21220fb9027de4be1d8cee7c4c6b8811705c54230c61b3a2d08bd`;
this controller was source-verified inline, not a production bundle installation.

```sh
.venv/bin/python -m scripts.prepare_scanner_databases \
  --operator-config .gca-local/validation-transport.json \
  --reviewed-program-sha256 b7726790ee87433b7af1093488a58ad80e588b8639fe722f0ca4f2e051dffe3f \
  --expected-bundle-sha256 b84b3c9da1c21220fb9027de4be1d8cee7c4c6b8811705c54230c61b3a2d08bd \
  --expected-policy-sha256 afa4901baca18a451e704b275beaebd1c1efcc7edf521debdd5925d9027fda73 \
  --expected-registry-sha256 6eb2f72e3b1b901e1fd9387f144f0612985318070f32df9e0d5c82f84014f345
```

Default is read-only observation. Acquisition additionally requires `--acquire`
and a retained reviewed controller. Role selection is from the fixed catalog,
never a request-provided URL, command or database path. Uncertainty means inspect
the existing transfer/store, not repeat an unknown executable action.

## Still open

Execute the verified scanner in a keyless gVisor profile using exact OCI inputs
and these freshly rechecked databases. Preserve language/OS coverage, bounded
native resources, reports/exit evidence and independent cleanup. Inspect findings,
remediation and exceptions before separate runtime approval. PostgreSQL still
needs its own pinned runtime input. These stages cannot be claimed from data
acquisition or green unit tests. Funded-controller activation, signing/publication,
behavioral verification and exact Chat URL delivery also remain open.

Local proof: 80 focused database/scanner/budget tests and all **1,680 coding tests**
passed without skips against loopback PostgreSQL (66.82 seconds). New-source lint,
medium/high security checks and whitespace validation passed. One existing Google
SDK deprecation warning remains. Synthetic tests and live data readback are distinct.
