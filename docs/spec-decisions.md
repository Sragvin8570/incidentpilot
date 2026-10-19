# IncidentPilot specification addendum

This file records decisions approved in project chat that were not yet reflected
in a specification file in the repository. It supplements the read-only v2.0
specification shared with the team.

## LLM provider

- Provider: Google Gemini through its OpenAI-compatible API.
- Base URL: `https://generativelanguage.googleapis.com/v1beta/openai/`.
- Model: read from the `LLM_MODEL` environment variable. Use
  `gemini-3.5-flash-lite` for development and set `LLM_MODEL=gemini-3.8-flash`
  for evaluation. The development model is the default; neither model is
  hard-coded in the investigator.
- API credentials: read from `LLM_API_KEY`; do not place credentials in this
  document, source control, or Kubernetes manifests.

## Verification gate

Do not start phase 4 (RCA and runbook retrieval) until the live verification for
phases 1–3 passes. A passing unit test, generated migration, or static manifest
check is not evidence that the live phase gate passed.

## Phase 5 policy

- Policy Option B is approved for production routing: an incident may be routed
  to `READY` only when its computed confidence meets the configured threshold,
  the recommended action has low or medium risk, and the target kind is not
  `StatefulSet`, `Node`, or `PersistentVolumeClaim`.
- Unlabeled or unrecognized namespaces are treated as production.
- `expand_pvc` always requires approval, including in development. High-risk
  actions and unknown targets/environments require approval.
- The `READY` state only marks a validated decision as eligible for a later
  execution phase; Phase 5 must not execute any action.
- Confidence is
  `0.2 * llm_confidence + 0.4 * grounding_score + 0.4 * runbook_score`.
  Weights and the default `0.8` threshold are configured in
  `config/policy.yaml`.
- `grounding_score` is the fraction of RCA claims whose non-empty evidence ID
  list refers entirely to successful (error-free) stored evidence rows, with at
  least one cited row referencing the incident target by resource name or
  owning workload. Empty claims or citations to missing/failed/unrelated
  evidence do not count as grounded. Per-claim citations, target references,
  result, and reason are persisted on the Decision.
- `runbook_score` is the highest cosine similarity returned by a successful
  `search_runbooks` result for the recommended action, clamped to `[0, 1]`.
  Missing results, a missing similarity, or a failed search contribute `0`.
