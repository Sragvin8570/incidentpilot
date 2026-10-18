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
