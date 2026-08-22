# Runtime model-provider boundary

## Invariant

Ordinary Workspace execution, composition and offline RAG evaluation use Gemini through
`RUNTIME_API_KEY`. Groq is restricted to coding and candidate engineering through the
separately sealed `CODING_GROQ_API_KEY`. The two credentials must never be equal, and the
runtime refuses any provider other than `gemini`.

Ollama continues to produce `nomic-embed-text` embeddings. Changing the generation model
does not change the stored 768-dimensional vectors and therefore requires no re-index.

## Runtime models

- fast: `gemini-2.5-flash`
- reasoning: `gemini-2.5-pro`
- bounded fallback: `gemini-2.5-flash-lite`

These are stable Gemini API model IDs. Stored legacy route labels are accepted only so an
in-flight pre-migration run can resolve to the equivalent Gemini tier.

## Governed release procedure

1. Create a Gemini API key dedicated to this application runtime.
2. Seal `RUNTIME_MODEL_PROVIDER=gemini` and `RUNTIME_API_KEY` in the Railway API and
   control-worker services. Do not add the coding key to the API.
3. Add `RUNTIME_API_KEY` as the GitHub Actions secret used by the weekly RAG evaluation.
4. Probe a bounded text completion and one read-only typed tool call in a non-production
   worker. Confirm model name, token accounting, timeout and rate-limit telemetry.

   ```bash
   RUNTIME_MODEL_PROVIDER=gemini RUNTIME_API_KEY='sealed-value' \
     python scripts/probe_runtime_model.py
   ```

   The command prints only provider/model/token facts and verifies the exact typed call;
   it never prints the key or sends a Workspace write.
5. Deploy the API and control worker together. Existing deterministic Workspace paths
   remain unchanged.
6. Verify a composition run, a live read, an approval-gated write and a semantic RAG run.
7. Delete `GROQ_API_KEY` and the old `GROQ_*` runtime variables from API, control worker,
   candidate worker and GitHub Actions. Keep only `CODING_GROQ_API_KEY` on the isolated
   coding/candidate service.
8. Roll back by restoring the prior application image, not by sharing the coding key with
   the application runtime.

The migration must not be deployed before step 2 because missing runtime credentials
would make model-dependent requests fail even though deterministic operations still work.
