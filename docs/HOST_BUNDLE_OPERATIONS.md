# Protected host bundle installation and rollback

This is an **operator** workflow, never a model-selected command or generated-app
tool. It updates reviewed stdlib files on the existing VM through pinned IAP/SSH.
It does not approve toolchain/runtime images, change policy or sudoers, prepare
resource groups, enroll financial capacity, run generated code or deploy apps.

## Installation protocol

```text
read-only probe: exact old-tree hash + idle host journal
        |
review seven source files; compile and test locally
        |
apply with observed old-tree hash
        |
same exclusive host slot + no unresolved jobs
        |
durable external intent -> staged complete bundle -> isolated import
        |
rename old root to protected rollback directory
        |                 crash here: gateway absent, fail closed
rename staged root to fixed gateway directory
        |
rehash + isolated import -> committed intent
        |
independent read-back + synthetic validation/replay/cleanup + health check
```

The two directory renames are **not** one atomic transaction. Each rename and the
intent are fsynced separately. If the process dies, a subsequent probe reports the
interrupted installation ID even when the gateway path is absent. Rollback checks
actual directory hashes, not just the last recorded phase. It never deletes the
host job journal, retained validation artifacts or data. The complete old bundle
is preserved; old bytecode caches are not copied into the new bundle.

Fixed defaults are `/usr/local/libexec/gca`, protected rollback storage at
`/usr/local/libexec/gca-bundle-releases`, and the existing state directory from
`/etc/gca-validation/policy.json`. Release count and payload sizes are bounded.
Unknown files, symlinks, hard links, unsafe ownership, changed hashes and occupied
rollback destinations fail closed. Retention exhaustion needs operator review;
this workflow does not silently prune old releases.

## Commands

Use the existing private operator configuration; never paste its keys into chat.
From the project root:

```sh
source .venv/bin/activate
python -m scripts.install_packaging_bundle \
  --operator-config .gca-local/validation-transport.json
```

This defaults to read-only inspection. Review its `snapshot_hash` and the exact
source/tests. Only then install:

```sh
python -m scripts.install_packaging_bundle \
  --operator-config .gca-local/validation-transport.json \
  --apply --expected-snapshot-hash "$OBSERVED_SNAPSHOT_HASH"
```

Only seven explicit known filenames are accepted. A lost response does **not**
authorize blind retry. Probe again; if it reports `interrupted=true`, recover the
exact installation:

```sh
python -m scripts.install_packaging_bundle \
  --operator-config .gca-local/validation-transport.json \
  --rollback "$OBSERVED_INSTALLATION_ID"
```

Rollback is refused while any job is unresolved or when packaging history/staging
exists. Restoring an old bundle after packaging could remove cleanup capability.
That state requires a reviewed forward-compatible repair, not deleting records or
using a bypass flag.

## Live evidence — 4 October 2026

Installed on the existing `gca-coolify-delhi-01` VM without a new resource, policy
change or production admission activation. Installation ID:
`ec87d7d8e3db1fd9f51a912bd9e0a501e6a953cedfa2eef10c9cfc8a0a4a77d2`.
Independent read-back snapshot:
`b03668e4cbbd2fe9567f24349a0efc3ae84ff7540193a38b0a149c5c8b54145f`.

| Reviewed file | Installed SHA-256 |
| --- | --- |
| isolated_validation.py | 55e35cea3acdf85d4465835e19a39d06d33c839e3ef9461d66d62e357265b6e1 |
| build_artifact.py | 1a6e04a0e56f0893335bd9d6476b794e01cd4c563d9dd9e1fe7d4cf9136fdc0d |
| oci_packager.py | 8c92a48d70d038bede9c4c10d5bc3e15f2d7ff0381f412b620cb8462b57ce28c |
| runtime_image_store.py | 82bd1842abec3cdd715c176e3c63b0dbb7c7baf7d77871d3119e6df8f8186ec2 |
| image_output_store.py | b74cc87918b86ccd0a5e5ce6bd5d6d5d9fbe871e22f09b4e9e3e77032b2c2c98 |
| image_packaging_worker.py | 97762c16ba0350955ae87b36d05ad759029f4a1905f64d67afb65dd8afe2b206 |
| isolated_packaging.py | b82602a2ddad4ede6636c5af9744bbb563840ff9a3dd8cb6e862c5a1663f4631 |

Read-only GCE identity verified the original instance ID, e2-standard-4, RUNNING
status, no service account and unchanged STOP at `2026-10-19T17:24:32Z`. The
nonsensitive synthetic Python transport probe passed compilation, replay rejection
and exact cleanup observation. All six Coolify containers were healthy, the reaper
reported success/exit 0, and zero validation containers remained.

Local verification: 620 coding tests passed with real loopback PostgreSQL and no
skips. Nineteen installer tests include real filesystem/SQLite transactions, abrupt
`os._exit(97)` after each rename, missing phase writes, occupied locks, rejected
foreign data and real isolated imports. These prove process-death recovery, **not**
power-loss behavior, live host rollback or a live packaging worker.

Packaging policy/cgroup preparation, actual offline React/Spring toolchains/runtime
bases, image publication/protected signing, production migrations/funded enrollment,
worker integration and complete behavioral deployment/Chat proof remain unfinished.
See [OCI packaging](OCI_PACKAGING.md). The trial allocation and VM deadline were
not extended; current attributable billing is still required.
