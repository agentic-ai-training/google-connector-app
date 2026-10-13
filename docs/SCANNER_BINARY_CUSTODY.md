# Scanner custody on the existing VM: no execution or approval

Checkpoint 13 October 2026. The exact previously signature-verified Trivy binary
was transferred through pinned IAP/SSH to the existing Delhi VM, under its shared
journal and resource limits. It remains mode 0600 non-executable data. No scanner,
application, model, new cloud resource or expanded hosting term was started.

## Real retained evidence

- Binary: 172,765,344 bytes, SHA-256
  `93f9da8e4ba5e0c1c76d8234ed2494cf9afb0a96fd21953e424bb795f3299b8e`.
- Reviewed earlier genuine operator Sigstore receipt SHA-256:
  `712dc1f6a1d34e65d2c54390ba25da4942220e6bdbe9bf5ea8786b9927cfa112`.
  See [signature preparation](RUNTIME_SCANNER_PREPARATION.md). This destination
  custody operation does not pretend to independently repeat that cryptographic check.
- Fixed private directory:
  `/var/lib/gca-runtime-scanner-v1/<binary-sha256>/`.
- Reviewed transfer controller SHA-256:
  `83eaa6e1d1723dc8ad828b711ed9b633c1d524f0879057496651538d6e85d406`.
- Retained Git blob: `24d1fcee459476370321d2a5b1996119495f739a`, under
  `refs/gca/scanner-custody/<controller-sha256>`.
- Installed host source remained
  `b84b3c9da1c21220fb9027de4be1d8cee7c4c6b8811705c54230c61b3a2d08bd`.

The actual pre-transfer probe found no custody store. The one transfer returned
the complete exact byte count/hash and retained immutable custody receipt. A later
independent read-only observation rehashed the actual complete binary and checked
the exact retained receipt and private/non-executable inode, confirming the same
installed host source. Neither operation ran the scanner or approved a runtime.

## Implemented boundaries

`app/coding/scanner_custody.py` rejects public/executable files, symlinks, hard
links, wrong ownership, oversized data and simultaneous complete/partial states.
The local source is rehashed against both the binary pin and the source-reviewed
hash of the real signature receipt. A caller-supplied `signature_verified: true`
boolean is not enough. No shell command, path, release version, executable pin or
provider credential can be chosen by a model.

`scripts/transfer_runtime_scanner.py` defaults to read-only observation. Mutation
requires a retained reviewed controller. The fixed remote controller verifies the
same VM, installed source, policy/registry, shared journal availability, fixed
STOP and existing one-CPU / 3-GiB / no-swap / 128-task aggregate before transfer.
It requires four GiB of existing free disk headroom and a 270-second alarm.
The binary is streamed directly from a protected local descriptor through IAP;
there is no extra 173-MB temporary payload file or whole-body Python allocation.

Recovery compares the remote partial length/hash with the corresponding verified
local prefix before appending. The receiver checks that exact continuation state
again and verifies the complete digest before promotion. Interrupted and corrupt
transfers remain unapproved partials. Completed partials can reconcile with zero
additional binary bytes. Custody plan/receipt files are immutable and bounded;
the activity never makes a host binary executable or installs it in a system PATH.

```sh
# Read-only independent custody observation; no re-transfer or execution.
.venv/bin/python -m scripts.transfer_runtime_scanner \
  --operator-config .gca-local/validation-transport.json \
  --reviewed-program-sha256 83eaa6e1d1723dc8ad828b711ed9b633c1d524f0879057496651538d6e85d406 \
  --expected-bundle-sha256 0d06e31b5b8b07faccdebc46bc0b508ad9aea3542a925de75f4597e755bb1a49 \
  --expected-policy-sha256 afa4901baca18a451e704b275beaebd1c1efcc7edf521debdd5925d9027fda73 \
  --expected-registry-sha256 6eb2f72e3b1b901e1fd9387f144f0612985318070f32df9e0d5c82f84014f345
```

Adding `--transfer` explicitly enters custody transfer, not execution. It first
observes the existing destination and compares its prefix. Do not re-transfer
merely because a previous observation request timed out; inspect retained state.
The command uses the subsequent current host snapshot recorded in
`RUNTIME_SCAN_JOURNAL.md`; the original transfer's source-bound plan/receipt was
not rewritten. A new-source mutation cannot silently replace that immutable plan.

## Next execution obligation: preserve coverage within the approved volume

The two expanded databases together exceed the existing 2,040-MiB per-job volume.
Do not increase host resources or drop Java identification to make a scan pass.
Trivy documents [CycloneDX inventory generation and subsequent SBOM scanning](https://trivy.dev/docs/latest/supply-chain/sbom/),
[exact OCI-layout input](https://trivy.dev/docs/latest/target/container_image/#oci-layout)
and [distinct Java identification versus vulnerability databases](https://trivy.dev/docs/latest/configuration/db/).
These support investigating a sequential inventory-to-vulnerability workflow.

The intended stages must bind the exact image manifest, inventory/report hashes,
scanner/database versions and language/OS coverage. Inventory generation must not
be represented as a vulnerability scan. Actual native gVisor execution, no-network
enforcement, current database freshness, independent cleanup and coverage proof
remain unimplemented/unverified at this checkpoint. PostgreSQL still needs its own
pinned input. Findings require remediation or separately reviewed exceptions;
runtime approval, funded deployment and exact Chat URL delivery remain separate.

Verification: 38 focused custody/database tests and all **1,697 coding tests** passed
without skips against loopback PostgreSQL (68.19 seconds). New-source Flake8,
medium/high Bandit and whitespace checks passed. One existing Google SDK deprecation
warning remains. Mocked test transfers are distinct from the actual IAP transfer.
