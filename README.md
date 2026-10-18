# IncidentPilot

IncidentPilot is a Kubernetes incident-response system built with Python/FastAPI and
React/TypeScript. Kubernaut is a design reference only; it is not installed or used
at runtime.

The build follows the nine phases in the project specification. The current
foundation includes local PostgreSQL/pgvector and Redis services, API health and
readiness endpoints, an initial incident schema, authenticated Alertmanager
ingestion with deduplication and enrichment, and a bounded read-only evidence
investigator. Live cluster validation is still pending.

Values called proposed defaults in the specification (including the LLM model,
confidence threshold, and approval expiry) remain configurable and are not
represented as team-approved decisions.

## Prerequisites

Use Linux/macOS, or Windows with WSL2 and VS Code's WSL extension. Install Docker,
Kind, kubectl, Helm, Python 3.12, Node.js 20+, and make. Start Docker before using
the local services or cluster.

## Local development

1. Copy `.env.example` to `.env` and set a unique `POSTGRES_PASSWORD`,
   `DATABASE_URL` with the matching password, and a long random `JWT_SECRET`.
   PostgreSQL is published only on `127.0.0.1:5433` by default to avoid conflicts
   with host-installed PostgreSQL; its container port remains 5432.
   Keep `.env` out of version control. Set `LLM_API_KEY` for the investigator.
   Set `ALERTMANAGER_WEBHOOK_TOKEN` to a separate random secret before
   configuring the Alertmanager receiver; the webhook requires a matching bearer
   authorization header.

   The monitoring values enable selection of the project's AlertmanagerConfig.
   `deploy/prometheus/incidentpilot-alertmanager-config.yaml` sends matching
   `team=incidentpilot` alerts to the local API and reads its bearer token from the
   `incidentpilot-alertmanager-webhook-token` Secret in the `monitoring` namespace.
   The live verification script creates or updates this Secret directly through
   the Kubernetes API from the local `.env`; it does not print the token or store
   it in a manifest.
2. Start the local data services:

   ```sh
   docker compose up -d postgres redis
   ```

3. Create a Python 3.12 virtual environment, install the backend dependencies,
   and run the initial migration:

   ```sh
   python3.12 -m venv .venv
   . .venv/bin/activate
   pip install -r backend/requirements.txt
   alembic -c backend/alembic.ini upgrade head
   uvicorn app.main:app --app-dir backend --reload
   ```

   In PowerShell, create and use the environment with
   `py -3.12 -m venv .venv`, `.venv\Scripts\Activate.ps1`, then
   `python -m pip install -r backend\requirements.txt`. The Python launcher
   command is `py -3.12`.
4. Check `http://localhost:8000/healthz` and `/readyz`. Readiness requires both
   PostgreSQL and Redis to be reachable.
5. Run backend tests with `pytest backend/tests`.

The Vite dashboard shell is in `frontend/`; its incident and approval pages are
added in the dashboard phase. To run it, install Node.js 20+ and start Vite from that
directory:

```sh
cd frontend
npm install
npm run dev
```

## Local Kubernetes foundation

Create the Kind cluster and install the monitoring stack:

```sh
kind create cluster --name incidentpilot --config deploy/kind/cluster.yaml
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update
# Use the repo values so the team route can match alerts from dev.
helm upgrade --install monitoring prometheus-community/kube-prometheus-stack `
  --namespace monitoring --create-namespace `
  --values deploy/prometheus/values.yaml
docker build -t incidentpilot-demo:local demo
kind load docker-image incidentpilot-demo:local --name incidentpilot
kubectl apply -f deploy/kubernetes/namespaces.yaml
kubectl apply -f deploy/kubernetes/demo.yaml
kubectl apply -f deploy/prometheus/demo-servicemonitor.yaml
```

The PowerShell continuation character above is a backtick. In a POSIX shell,
replace it with `\`.

The demo service exposes a basic application endpoint and Prometheus metrics in
both `dev` and `prod`. This is only a scrape target; alert rules and incident
handling are added in later phases.

## Alertmanager webhook (phase 2)

The API accepts Alertmanager webhook payloads at
`POST /api/v1/webhooks/alertmanager`. It records firing alerts as incidents,
deduplicates open incidents by fingerprint, enriches scoped alerts from live
Kubernetes namespace/workload data, and stores resolved notifications for later
verification. Alerts without a namespace or a supported, existing target are
recorded as `UNSCOPED` and are never sent for investigation. Unknown or unlabeled
namespace environments default to production.

This endpoint returns `503` until `ALERTMANAGER_WEBHOOK_TOKEN` is configured.
The backend also needs Kubernetes API access to enrich scoped alerts; without a
working Kubernetes client configuration it returns `503` rather than accepting
an unenriched incident as safe. A valid pod target is resolved through its
ReplicaSet owner to the Deployment when applicable.

Scoped incidents are queued for evidence collection through Celery/Redis. Run a
worker from the repository root with:

```sh
celery --workdir backend -A app.workers.celery_app:celery_app worker --loglevel=INFO
```

Run Celery beat as a separate process (Celery's embedded `--beat` mode is not
supported on Windows) to periodically escalate investigations that exceed
`INVESTIGATION_TIMEOUT_SECONDS`:

```sh
celery --workdir backend -A app.workers.celery_app:celery_app beat --loglevel=INFO
```

Configure `LLM_BASE_URL`, `LLM_API_KEY`, and `LLM_MODEL` for an OpenAI-compatible
provider. Configure `PROMETHEUS_URL` to reach Prometheus; when running the API
outside the cluster, port-forward the Prometheus service to `localhost:9090`.
The default development model is `gemini-3.5-flash-lite`; set `LLM_MODEL=gemini-3.8-flash`
for evaluation.
The investigator uses bounded, read-only Kubernetes and Prometheus tools, stores
sanitized tool results as evidence, and escalates on LLM errors or investigation
limits. A valid structured RCA is checked against stored evidence and the action
catalog before an incident reaches `ANALYZED`. RCA recommends actions only; this
phase does not execute remediation or resolve incidents.

## Live phase 1–3 verification

After Docker Desktop, Kind, and Helm are ready, install the project
dependencies, set the local `.env` values, and start phase verification from the
repository root:

```powershell
.\.venv\Scripts\python.exe backend\scripts\verify_phases_1_3.py --confirm-live
```

The opt-in script starts the local database, Redis, API, and Celery worker;
creates or updates the `incidentpilot` Kind cluster and monitoring chart;
applies the webhook Secret and Alertmanager configuration; checks that
Prometheus scrapes the demo app; and deploys a uniquely named CrashLoop workload
in `dev`. It reports PASS/FAIL evidence for the phase 1–3 `done_when` gates and
removes only that temporary crash-loop Deployment. Do not start phase 4 unless
all three live gates pass.

## Phase 4: runbooks and RCA

Runbook Markdown lives in `runbooks/` and links to an action through its
`linked_action_id` front-matter value. Actions and their parameter schemas,
bounds, target kinds, and risk levels live in `config/actions/`. Install the
backend requirements, apply migrations, then ingest the heading-based chunks
using the local CPU embedding model:

```powershell
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
.\.venv\Scripts\python.exe -m alembic -c backend\alembic.ini upgrade head
.\.venv\Scripts\python.exe backend\scripts\ingest_runbooks.py
```

The embedding model defaults to `sentence-transformers/all-MiniLM-L6-v2` and
runs on CPU. The investigator exposes `search_runbooks` and
`find_similar_incidents`; RCA uses strict structured output, validates all
claim evidence IDs and action parameters, and escalates after one invalid-output
retry. Start the worker with `--beat` for the investigation-timeout watchdog.

After phases 1–3 have passed live, verify phase 4 without starting phase 5:

```powershell
.\.venv\Scripts\python.exe backend\scripts\verify_phase_4.py --confirm-live
```

The verifier runs two isolated CrashLoop scenarios: a deterministic `exit 1`
with one Deployment revision (expected `none` and escalation with the RCA
attached), and a healthy revision followed by a failing revision (expected
`rollback_deployment`, citing `get_rollout_history`). Each case checks that
every RCA claim cites existing evidence and removes only its temporary
Deployment. No recommended action is executed.

The earlier transient test was not a real dependency outage: its BusyBox
command printed a dependency-failure message, exited with status 42 for its
first three starts, then exited successfully on the fourth. It did not connect
to or depend on a separate service, so it is not counted as a phase-4
acceptance scenario.

To rerun only the phase 3 live investigation after phases 1 and 2 are already
verified, use:

```powershell
.\.venv\Scripts\python.exe backend\scripts\verify_phase_3.py --confirm-live
```

This creates one uniquely fingerprinted test incident through the local API
against the existing `dev/Deployment/incidentpilot-demo`, then requires Gemini
to select real read-only tools and complete the investigation. It does not
recreate the cluster, reapply phase 1–2 resources, or delete the resulting
incident/evidence/audit record.

## Security and safety

- Do not commit credentials or `.env`.
- The agent must not execute LLM-generated shell or kubectl commands. Remediation
  will be restricted to validated catalog actions and least-privilege identities.
- Unlabeled namespaces and unknown targets must be treated as high risk.
- An incident must not be marked resolved until verification passes.
