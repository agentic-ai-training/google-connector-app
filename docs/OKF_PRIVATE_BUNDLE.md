# Protected OKF bundle

Public operational knowledge belongs in `knowledge/`. Private organizational
knowledge must never be committed to this public repository.

Mount a protected directory into the API and worker, set
`OKF_PRIVATE_BUNDLE_PATH` to that directory, and use the same Markdown/YAML
frontmatter schema as the public bundle. Every protected document must declare:

```yaml
visibility: private
publication_status: approved
approved_by: accountable-human-id
approved_at: 2026-07-20T00:00:00Z
```

The loader namespaces protected IDs under `private/`, validates links, registered
tools, secrets, approval metadata, and schema, and stores them separately by
visibility. Ordinary runtime retrieval always filters to public documents.
Private retrieval requires the caller to pass `include_private=True`; that option
must only be used after an administrator authorization check. No current user
request path enables it.
