# Complete New System Setup Guide for IncidentPilot

This document is a step-by-step, zero-to-running setup guide for deploying **IncidentPilot** on a fresh development machine.

---

## 1. System Requirements & Prerequisites

### Minimum Hardware
* **CPU:** 4+ cores (recommended 8+ cores)
* **RAM:** 8 GB minimum (16 GB recommended for running Kind, Prometheus, and local LLM/embeddings simultaneously)
* **Disk:** At least 20 GB of free space
* **Virtualization:** Hardware virtualization (VT-x / AMD-V) enabled in BIOS/UEFI.

### Supported Operating Systems
* **Windows 10 / 11:** 64-bit with WSL2 (Windows Subsystem for Linux 2)
* **Linux:** Ubuntu 22.04 LTS / Debian 12 / Fedora 39+
* **macOS:** macOS Sonoma / Ventura (Apple Silicon or Intel)

---

## 2. Install Required Tools

Install the following software packages on your host system:

### On Windows (using `winget` or direct installers)
Open PowerShell as Administrator:

```powershell
# 1. Docker Desktop
winget install Docker.DockerDesktop

# 2. Kubernetes Tools (kubectl, Kind, Helm)
winget install Kubernetes.kubectl
winget install Kubernetes.kind
winget install Helm.Helm

# 3. Python 3.12
winget install Python.Python.3.12

# 4. Node.js (LTS version 20+)
winget install OpenJS.NodeJS.LTS

# 5. Git
winget install Git.Git
```

> **Important Windows Configuration:**
> * Open **Docker Desktop** $\rightarrow$ **Settings** $\rightarrow$ **General** $\rightarrow$ check **"Use the WSL 2 based engine"**.
> * Allow PowerShell script execution (run once):
>   ```powershell
>   Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
>   ```

### On Linux (Ubuntu / Debian)
```bash
# Update package list
sudo apt-get update && sudo apt-get install -y curl git make build-essential

# 1. Docker Engine & Compose
sudo apt-get install -y docker.io docker-compose-v2
sudo usermod -aG docker $USER

# 2. kubectl
curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
sudo install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl && rm kubectl

# 3. Kind
curl -Lo ./kind https://kind.sigs.k8s.io/dl/v0.25.0/kind-linux-amd64
chmod +x ./kind && sudo mv ./kind /usr/local/bin/kind

# 4. Helm
curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash

# 5. Python 3.12
sudo apt-get install -y python3.12 python3.12-venv python3-pip

# 6. Node.js (LTS 20+)
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt-get install -y nodejs
```

### On macOS (using Homebrew)
```bash
brew install git make docker kubectl kind helm python@3.12 node@20
```

---

## 3. Step-by-Step Project Setup

### Step 1: Clone the Repository
```bash
git clone <repository-url> major-project-1-7th-sem
cd major-project-1-7th-sem
```

---

### Step 2: Configure Environment Variables (`.env`)
Create your local `.env` from the provided example:

```powershell
# Windows PowerShell:
Copy-Item .env.example .env

# Linux / macOS:
cp .env.example .env
```

Open `.env` in an editor and set your configuration:

```ini
# Database & Redis Settings
POSTGRES_USER=incidentpilot
POSTGRES_PASSWORD=generate-a-strong-password-here
POSTGRES_DB=incidentpilot
POSTGRES_PORT=5433
DATABASE_URL=postgresql+asyncpg://incidentpilot:generate-a-strong-password-here@localhost:5433/incidentpilot
REDIS_URL=redis://localhost:6379/0

# Security Secrets (Generate random 32+ character secrets)
JWT_SECRET=generate-a-long-random-jwt-secret-string-32-chars
ALERTMANAGER_WEBHOOK_TOKEN=generate-a-random-webhook-secret-token

# Human Approver Account (For Web Dashboard Login)
APPROVER_USERNAME=approver
APPROVER_PASSWORD=IncidentPilotApprover2026!
ACCESS_TOKEN_EXPIRY_MINUTES=60
APPROVAL_EXPIRY_MINUTES=30

# LLM Provider Configuration (Google Gemini OpenAI-compatible endpoint)
LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
LLM_API_KEY=your-gemini-api-key-here
LLM_MODEL=gemini-3.5-flash-lite

# URLs for Monitoring Stack (Port-Forwarded or Cluster-Reachable)
PROMETHEUS_URL=http://localhost:9090
ALERTMANAGER_URL=http://localhost:9093
INVESTIGATION_TIMEOUT_SECONDS=300
RUNBOOK_EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2
```

> 💡 **Tip to generate random secrets:**
> ```powershell
> python -c "import secrets; print(secrets.token_hex(32))"
> ```

---

### Step 3: Start Docker Services (PostgreSQL & Redis)
Ensure Docker is running, then start the containerized database and queue:

```powershell
docker compose up -d postgres redis
```

Verify they are healthy:
```powershell
docker compose ps
```
You should see both containers in `Up (healthy)` state.

---

### Step 4: Set up the Python Backend Environment

1. **Create and activate the Python 3.12 virtual environment:**

   * **Windows PowerShell:**
     ```powershell
     py -3.12 -m venv .venv
     .\.venv\Scripts\Activate.ps1
     ```
   * **Linux / macOS:**
     ```bash
     python3.12 -m venv .venv
     source .venv/bin/activate
     ```

2. **Install backend Python dependencies:**
   ```powershell
   python -m pip install --upgrade pip
   pip install -r backend/requirements.txt
   ```

3. **Run database migrations:**
   ```powershell
   alembic -c backend/alembic.ini upgrade head
   ```

4. **Seed the Approver user account:**
   ```powershell
   python backend/scripts/seed_approver.py
   ```
   *(Outputs: `Approver account ready: approver`)*

5. **Ingest Runbook Chunks & Vector Embeddings:**
   ```powershell
   python backend/scripts/ingest_runbooks.py
   ```

---

### Step 5: Initialize the Local Kubernetes Cluster (Kind)

1. **Create the Kind cluster:**
   ```powershell
   kind create cluster --name incidentpilot --config deploy/kind/cluster.yaml
   ```

2. **Install the Prometheus / Alertmanager monitoring stack:**
   ```powershell
   helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
   helm repo update

   helm upgrade --install monitoring prometheus-community/kube-prometheus-stack `
     --namespace monitoring --create-namespace `
     --values deploy/prometheus/values.yaml
   ```
   *(On Linux/macOS, replace the trailing backtick `` ` `` with `\`)*

3. **Create namespaces (`dev`, `staging`, `prod`):**
   ```powershell
   kubectl apply -f deploy/kubernetes/namespaces.yaml
   ```

4. **Build and load the Demo and Executor container images into Kind:**
   ```powershell
   # 1. Demo workload image
   docker build -t incidentpilot-demo:local demo
   kind load docker-image incidentpilot-demo:local --name incidentpilot

   # 2. Kubernetes Executor image
   docker build -t incidentpilot-executor:local -f backend/Dockerfile.executor backend
   kind load docker-image incidentpilot-executor:local --name incidentpilot
   ```

5. **Deploy the demo service and RBAC roles:**
   ```powershell
   kubectl apply -f deploy/kubernetes/demo.yaml
   kubectl apply -f deploy/prometheus/demo-servicemonitor.yaml
   python backend/scripts/install_executor_rbac.py
   ```

---

### Step 6: Setup the React Frontend

1. **Navigate to the `frontend/` directory:**
   ```powershell
   cd frontend
   ```

2. **Install Node.js dependencies:**
   ```powershell
   npm install
   ```

3. **Verify the frontend production build:**
   ```powershell
   npm run build
   ```
   *(Should compile with 0 errors and output built assets into `dist/`)*

4. Return to the project root:
   ```powershell
   cd ..
   ```

---

## 4. Running the Complete System

To run the full stack during local development, open **4 separate terminals**:

### Terminal 1: Backend FastAPI API Server
```powershell
.\.venv\Scripts\Activate.ps1
uvicorn app.main:app --app-dir backend --reload --port 8000
```
* **Health Check:** [http://localhost:8000/healthz](http://localhost:8000/healthz)
* **Readiness Check:** [http://localhost:8000/readyz](http://localhost:8000/readyz) $\rightarrow$ returns `{"status":"ready"}`.

### Terminal 2: Celery Worker (Investigation & Execution Engine)
```powershell
.\.venv\Scripts\Activate.ps1
celery --workdir backend -A app.workers.celery_app:celery_app worker --loglevel=INFO
```

### Terminal 3: Celery Beat (Scheduler & Approval Expiry Watchdog)
```powershell
.\.venv\Scripts\Activate.ps1
celery --workdir backend -A app.workers.celery_app:celery_app beat --loglevel=INFO
```

### Terminal 4: React Web Dashboard (Vite)
```powershell
cd frontend
npm run dev
```

---

## 5. Accessing the Web Dashboard

1. Open your browser and navigate to: **[http://localhost:5173](http://localhost:5173)**
2. Log in using your approver credentials:
   * **Username:** `approver` *(or your configured `APPROVER_USERNAME`)*
   * **Password:** `IncidentPilotApprover2026!` *(or your configured `APPROVER_PASSWORD`)*
3. Explore:
   * **Dashboard (`/`)**: Cluster readiness monitor, KPI metric cards, and pending approval alerts.
   * **Incidents Catalog (`/incidents`)**: Filter by state, severity, or search alert names.
   * **Incident Deep-Dive (`/incidents/:id`)**: 8-stage pipeline tracker, Root Cause Analysis claims, evidence, remediation Jobs, and verification checks.
   * **Pending Approvals (`/approvals`)**: Authorize or reject production/stateful workloads with live countdown timers.

---

## 6. Verifying the System (Automated Tests)

Run the backend unit test suite:
```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests
```

Run the live Kubernetes verification gate for approvals:
```powershell
.\.venv\Scripts\python.exe backend/scripts/verify_phase_7a.py --confirm-live
```

---

## 7. Troubleshooting & Common Gotchas

| Issue | Cause | Solution |
| :--- | :--- | :--- |
| **`docker: error during connect`** | Docker Desktop is not started | Start Docker Desktop from the Start Menu and wait for the green icon. |
| **Port `5433` already allocated** | Another PostgreSQL container or host process is using port 5433 | Change `POSTGRES_PORT=5434` in `.env` and update the port in `DATABASE_URL`. |
| **`cannot be loaded because running scripts is disabled`** | Windows PowerShell execution policy restriction | Run `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser`. |
| **`readyz` returns 503 "dependencies: ['database']"** | Postgres container is stopped or still starting | Run `docker compose ps` and `docker compose logs postgres`. Ensure password in `DATABASE_URL` matches `POSTGRES_PASSWORD`. |
| **`readyz` returns 503 "dependencies: ['redis']"** | Redis container is stopped | Run `docker compose up -d redis`. |
| **Celery worker crashes on Windows** | Attempting to run worker with embedded beat (`-B`) | On Windows, Celery worker and Celery beat **must** run in two separate terminals. |

