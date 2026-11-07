# 🐳 Live Container Testing: Nyaya-Mitra on IncidentPilot

Your real project from `C:\btp_6_sem\btp_6_sem\backend` is now **containerized with Docker** and **running live inside your Kubernetes cluster**.

---

## 🔍 Verify Your Real Container Running in Kubernetes

```powershell
# Check the pod status:
kubectl get pods -n dev -l app=nyaya-mitra-backend

# View the real application logs directly from the container:
kubectl logs -n dev -l app=nyaya-mitra-backend
```

Output:
```
Nyaya-Mitra Backend running on http://localhost:3000
```

---

## 💥 How to Test IncidentPilot Healing Your Real Container

You can trigger a real container failure in one of three ways:

### Test 1: Bad Rollout (Broken Tag / Crash)
Deploy a broken update to see IncidentPilot diagnose the rollout and automatically roll it back:
```powershell
kubectl set image deployment/nyaya-mitra-backend nyaya-mitra-backend=nyaya-mitra-backend:broken-v2 -n dev
```

### Test 2: CrashLoopBackOff (Broken Config)
Inject an invalid startup command so the container crashes on boot:
```powershell
kubectl set env deployment/nyaya-mitra-backend PORT=invalid_port_string -n dev
```

### Test 3: Out-of-Memory (OOMKilled)
Restrict memory so heavy document parsing triggers an OOM:
```powershell
kubectl set resources deployment/nyaya-mitra-backend --limits=memory=32Mi -n dev
```

---

## 📊 Where to Watch IncidentPilot Investigate Live

Open your browser at:
**[http://localhost:5173/incidents](http://localhost:5173/incidents)**

IncidentPilot will:
1. Ingest the alert from Prometheus.
2. Read the live pod logs from your Nyaya-Mitra container.
3. Diagnose the exact failure using its LLM reasoning and runbooks.
4. Calculate the **95% Bootstrap Confidence Interval**.
5. Roll back the bad revision or adjust resources to restore your container to a healthy state!
