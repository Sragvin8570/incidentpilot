# IncidentPilot

IncidentPilot is a Kubernetes incident-response system built with Python/FastAPI and
React/TypeScript. Kubernaut is a design reference only; it is not installed or used
at runtime.

The build follows the nine phases in the project specification. The current
foundation includes local PostgreSQL/pgvector and Redis services, API health and
readiness endpoints, an initial incident schema, authenticated Alertmanager
ingestion with deduplication and enrichment, and a bounded read-only evidence
investigator. Live cluster validation is still pending.

The investigation model remains configurable through `LLM_MODEL`. Decision
confidence weights, threshold, and risk-routing rules are configured in
`config/policy.yaml`.

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

## Phase 7a: authentication and approvals (backend)

Set `APPROVER_USERNAME` and a unique, long `APPROVER_PASSWORD` in `.env`.
After applying migrations, the API seeds this account as an approver on startup;
the standalone `backend\scripts\seed_approver.py` command can seed it explicitly.
Passwords are stored as salted PBKDF2 hashes and are never included in manifests
or source control. JWT access tokens are issued by `POST /api/v1/auth/login`;
`ACCESS_TOKEN_EXPIRY_MINUTES` controls their lifetime. The pending approvals
endpoint is `GET /api/v1/approvals/pending`. Authenticated approver/admin users
can `POST /api/v1/approvals/{incident_id}/approve` or reject with a required
`{"comment":"..."}` body at `/api/v1/approvals/{incident_id}/reject`. Every
decision audit records the acting username, role, and rejection comment.
Approvals expire after `APPROVAL_EXPIRY_MINUTES` (default 30); API reads and
the Celery beat expiry task transition expired incidents through `EXPIRED` to
`ESCALATED`. Approved incidents use the existing phase-6 preflight, snapshot,
RBAC-scoped Job, and idempotent execution flow.

Run the live phase-7a gates only with an initialized Kind cluster and the
approver environment variables configured:

```powershell
.\.venv\Scripts\python.exe -m alembic -c backend\alembic.ini upgrade head
.\.venv\Scripts\python.exe backend\scripts\verify_phase_7a.py --confirm-live
```

The gate checks an approved production StatefulSet reaches `VERIFYING` after
its Job succeeds, a viewer receives HTTP 403 when attempting approval, and a
second production StatefulSet is rejected and escalated without a Job. It
cleans up only its uniquely named workloads, Jobs, and temporary viewer.

## Phase 7b: React dashboard (frontend)

The React/TypeScript dashboard provides operational visibility into autonomous
incident handling and human-in-the-loop approval workflows:

1. **Authentication**: Login screen with JWT token issuance (`POST /api/v1/auth/login`)
   and role-based authorization (`viewer`, `approver`, `admin`).
2. **Operations Dashboard**: Real-time KPI statistics (active, awaiting approval,
   executing/verifying, resolved, escalated), cluster readiness health monitor,
   pending approval quick-action alerts, and recent incidents.
3. **Incidents Catalog**: Filterable table with state, severity, and text search,
   pagination, and direct links to full incident inspection.
4. **Incident Deep-Dive**: Visual pipeline stage tracker (Intake → Enrichment →
   Investigation → RCA → Policy Decision → Execution → Verification → Outcome),
   tabbed views for Root Cause Analysis (claims & citations), Policy Decision
   (confidence scores breakdown & risk evaluation), Remediation Job (pod logs &
   target hashes), Closed-Loop Verification (health checks & effectiveness score),
   read-only Evidence viewer, and an Audit Trail timeline.
5. **Human Approvals**: Dedicated approvals queue with countdown timers before expiry,
   one-click execution approval, and rejection modal with required comment audit logging.

To run the dashboard during development:

```sh
cd frontend
npm install
npm run dev
```

To build for production:

```sh
cd frontend
npm run build
```

The earlier transient test was not a real dependency outage: its BusyBox
command printed a dependency-failure message, exited with status 42 for its
first three starts, then exited successfully on the fourth. It did not connect
to or depend on a separate service, so it is not counted as a phase-4
acceptance scenario.

## Phase 5: policy decisions

`config/policy.yaml` defines the confidence weights and threshold plus ordered
risk-scoring rules. A persisted Decision records the LLM, evidence-grounding,
and runbook components, the weighted confidence, highest matching risk score,
and routing reason. Unlabeled environments are treated as production;
production, StatefulSet targets, high-risk actions, and `expand_pvc` require
approval. A confident low/medium-risk development recommendation is persisted
as `READY`; this state does not execute the action.

After applying the phase-5 migration, run the live policy gates without
starting phase 6:

```powershell
.\.venv\Scripts\python.exe -m alembic -c backend\alembic.ini upgrade head
.\.venv\Scripts\python.exe backend\scripts\verify_phase_5.py --confirm-live
```

The verifier sends scoped test alerts for a failing Deployment rollout in
`dev` and `prod`, and a transiently failing StatefulSet in `prod`. It checks
each persisted Decision and removes only its temporary workload. It does not
execute any recommendation.

## Phase 6: bounded Kubernetes execution

Only `READY` incidents are eligible for execution. `AWAITING_APPROVAL` and
`APPROVED` incidents are not consumed or changed in this phase. Build and load
the repository-owned Python/Kubernetes executor image into Kind, install one
action-specific ServiceAccount and namespaced Role in each supported
environment, then upgrade the database:

```powershell
docker build -t incidentpilot-executor:local -f backend\Dockerfile.executor backend
kind load docker-image incidentpilot-executor:local --name incidentpilot
.\.venv\Scripts\python.exe backend\scripts\install_executor_rbac.py
.\.venv\Scripts\python.exe -m alembic -c backend\alembic.ini upgrade head
```

The installer only creates the configured action identities in `dev`,
`staging`, and `prod`. Each Job runs as the account for its action; its Role is
namespaced and grants only the target resource's required `get` and `patch`
verbs. The Celery worker's Kubernetes identity must be able to read targets,
perform server-side dry-run patches, create/watch Jobs, and retrieve their pod
logs. Configure `EXECUTOR_IMAGE` and `EXECUTION_TIMEOUT_SECONDS` when using
non-default values. Before creating a Job, the worker stores the target spec
and SHA-256 hash and requires server-side dry-run success. An incident-unique
Execution reservation prevents a second Job on retries. Job success advances
`READY -> EXECUTING -> VERIFYING`; failures and timeouts are recorded with Job
logs and escalated. A retry of an incident with an Execution row never creates
another Job.

Run the focused execution tests and the complete backend suite from
`backend\` using `.venv\Scripts\python.exe -m pytest -q`. The live phase-6 gate
is run only after the local Kind/Docker cluster is available; it must verify a
failing `dev` Deployment rollback reaches ready pods on the previous revision,
while a production StatefulSet remains `AWAITING_APPROVAL` without a Job.
After building/loading the image, installing RBAC, and applying migrations, run
the opt-in live gate with:

```powershell
.\.venv\Scripts\python.exe backend\scripts\verify_phase_6.py --confirm-live
```

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

## Phase 8: closed-loop verification

Successful Jobs enter `VERIFYING` and automatically start closed-loop checks.
Each action in `config/actions` defines a stabilization window, alert grace
period, recovery PromQL expression, threshold, and comparison operator. The
worker confirms desired replicas are Ready with no restart-count increases,
checks the original Alertmanager fingerprint is no longer active, evaluates
the configured Prometheus recovery metric, and compares the current target
spec hash with the expected hash after the execution patch. All four checks
are stored in one Verification row with an effectiveness score from 0 to 1.
Passing checks resolve the incident; any failure records the failing checks
and escalates it. A periodic Celery task recovers missed verification dispatches.
`find_similar_incidents` includes completed effectiveness scores when present.

Set `PROMETHEUS_URL` and `ALERTMANAGER_URL` to reachable endpoints (the default
local ports are 9090 and 9093), apply migrations, then run the live gate:

```powershell
.\.venv\Scripts\python.exe -m alembic -c backend\alembic.ini upgrade head
.\.venv\Scripts\python.exe backend\scripts\verify_phase_8.py --confirm-live
```

The gate checks that a real dev CrashLoop alert is rolled back to the previous
healthy revision and resolves only after all checks pass. A second case reapplies
the failing revision during stabilization and must escalate with workload health
and change persistence failures. It removes only its named test Deployments and
executor Jobs.

## Phase 9: evaluation harness

Run the scenario matrix with the evaluation model from `docs/spec-decisions.md`.
Each successful row records expected-cause concept matches and a reason in
`rca_reason`; provider failures are `INVALID` and excluded from summary metrics.
LLM calls count successful model responses (investigation/tool turns and RCA
retries), not Kubernetes tool invocations. Re-running with `--resume` skips
only `VALID` scenario/run pairs from the same model/config CSV.

```powershell
.\.venv\Scripts\python.exe eval\run.py --scenario all --runs 3 --config full --model gemini-3.8-flash
.\.venv\Scripts\python.exe eval\run.py --scenario all --runs 3 --config full --model gemini-3.8-flash --resume
.\.venv\Scripts\python.exe eval\run.py --scenario all --runs 3 --config no_tools --model gemini-3.8-flash
.\.venv\Scripts\python.exe eval\run.py --scenario all --runs 3 --config no_verification --model gemini-3.8-flash
.\.venv\Scripts\python.exe eval\summarize.py
```

## Security and safety

- Do not commit credentials or `.env`.
- The agent must not execute LLM-generated shell or kubectl commands. Remediation
  will be restricted to validated catalog actions and least-privilege identities.
- Unlabeled namespaces and unknown targets must be treated as high risk.
- An incident must not be marked resolved until verification passes.
