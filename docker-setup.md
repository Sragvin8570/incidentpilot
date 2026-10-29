# Docker Setup Guide for IncidentPilot

This guide explains how Docker is used in IncidentPilot, how to install and configure it, and how to operate the containerized services and local Kubernetes cluster.

---

## 1. Prerequisites & Installation

### Windows
1. **Install Docker Desktop**: Download and install [Docker Desktop for Windows](https://www.docker.com/products/docker-desktop/).
2. **Enable WSL2 Backend**:
   * Open Docker Desktop **Settings** $\rightarrow$ **General** $\rightarrow$ check **Use the WSL 2 based engine**.
   * Under **Resources** $\rightarrow$ **WSL Integration**, enable integration with your default WSL distribution (e.g., Ubuntu).
3. **Verify installation**:
   ```powershell
   docker --version
   docker compose version
   ```

### Linux / macOS
* **macOS**: Install Docker Desktop for Mac with Apple Silicon or Intel support.
* **Linux**: Install Docker Engine and the `docker-compose-plugin`:
  ```sh
  sudo apt-get update && sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
  sudo usermod -aG docker $USER
  ```

---

## 2. Docker Architecture in IncidentPilot

IncidentPilot uses Docker in three ways:

```
┌─────────────────────────────────────────────────────────────────┐
│                      Docker Engine                              │
│                                                                 │
│  ┌──────────────────────┐  ┌─────────────────────────────────┐  │
│  │   docker-compose     │  │       Kind (K8s-in-Docker)      │  │
│  │                      │  │                                 │  │
│  │  • PostgreSQL 16     │  │  • Control-Plane node           │  │
│  │    (pgvector:pg16)   │  │  • Worker node                  │  │
│  │    Port: 5433        │  │                                 │  │
│  │                      │  │  Images Loaded:                 │  │
│  │  • Redis 7           │  │  • incidentpilot-demo:local     │  │
│  │    (alpine)          │  │  • incidentpilot-executor:local │  │
│  │    Port: 6379        │  │                                 │  │
│  └──────────────────────┘  └─────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

1. **Local Data Infrastructure (`docker-compose.yml`)**:
   * **PostgreSQL + pgvector (`pgvector/pgvector:pg16`)**: Stores incidents, decisions, audit logs, executions, and vector embeddings for runbook retrieval. Mapped to host port `5433` (to avoid conflicting with any existing host Postgres on 5432).
   * **Redis (`redis:7-alpine`)**: Task broker and results backend for Celery background tasks. Mapped to host port `6379`.
2. **Local Kubernetes Cluster (Kind)**:
   * Runs Kubernetes control-plane and worker nodes inside Docker containers (`kindest/node:v1.37.0`).
3. **Project Images**:
   * `incidentpilot-demo:local`: Scrape target workload providing metrics to Prometheus.
   * `incidentpilot-executor:local`: Python/Kubernetes executor image running isolated remediation Jobs.

---

## 3. Daily Operations: Postgres & Redis

The data services are managed through `docker-compose.yml`.

### Start Services
```powershell
docker compose up -d postgres redis
```

### Check Container Status & Health
```powershell
docker compose ps
```
You should see:
```text
NAME                                 IMAGE                    STATUS                    PORTS
major-project-1-7th-sem-postgres-1   pgvector/pgvector:pg16   Up (healthy)              127.0.0.1:5433->5432/tcp
major-project-1-7th-sem-redis-1      redis:7-alpine           Up (healthy)              0.0.0.0:6379->6379/tcp
```

### View Service Logs
```powershell
# Follow both services
docker compose logs -f

# View Postgres logs only
docker compose logs -f postgres

# View Redis logs only
docker compose logs -f redis
```

### Stop Services
```powershell
# Stop containers without losing data
docker compose stop

# Stop and remove containers (data volumes preserved)
docker compose down
```

### Reset Data (Clean Slate)
If you need to wipe the local database and Redis queues to start completely fresh:
```powershell
docker compose down -v
docker compose up -d postgres redis
.\.venv\Scripts\python.exe -m alembic -c backend\alembic.ini upgrade head
.\.venv\Scripts\python.exe backend\scripts\seed_approver.py
```

---

## 4. Building & Loading Custom Docker Images into Kind

When running the full Kubernetes pipeline or live verification gates, build the container images and load them directly into your local Kind cluster:

### 1. Build and load the Demo Workload
```powershell
docker build -t incidentpilot-demo:local demo
kind load docker-image incidentpilot-demo:local --name incidentpilot
```

### 2. Build and load the Remediation Executor Image
```powershell
docker build -t incidentpilot-executor:local -f backend\Dockerfile.executor backend
kind load docker-image incidentpilot-executor:local --name incidentpilot
```

---

## 5. Troubleshooting & Common Issues

### Issue 1: "Docker daemon is not running"
* **Symptom:** `error during connect: ... open //./pipe/docker_engine: The system cannot find the file specified.`
* **Fix:** Start the **Docker Desktop** application from your Windows Start Menu and wait for the whale icon in the taskbar to show green / "running".

### Issue 2: Port Conflict (Port 5433 or 6379 already in use)
* **Symptom:** `Bind for 127.0.0.1:5433 failed: port is already allocated`
* **Fix:** Find what process is using the port:
  ```powershell
  Get-NetTCPConnection -LocalPort 5433
  ```
  If needed, change `POSTGRES_PORT` in your `.env` to another port (e.g. `5434`) and update `DATABASE_URL` accordingly.

### Issue 3: Postgres is unhealthy or connections refused
* **Check container health:**
  ```powershell
  docker compose ps
  docker compose logs postgres
  ```
* **Verify password in `.env`:** Ensure `POSTGRES_PASSWORD` in `.env` matches the password in `DATABASE_URL`.

---

## 6. Quick Reference Cheat Sheet

| Task | Command |
| :--- | :--- |
| **Start DB & Redis** | `docker compose up -d postgres redis` |
| **Check Health** | `docker compose ps` |
| **Stop Services** | `docker compose stop` |
| **Wipe Data Volumes** | `docker compose down -v` |
| **View Redis CLI** | `docker compose exec redis redis-cli ping` |
| **View Postgres Shell** | `docker compose exec postgres psql -U incidentpilot -d incidentpilot` |
| **Inspect Kind Nodes** | `docker ps --filter "name=incidentpilot"` |
