# SPARK✨: BPPIMT Campus Resource Assistant

![SPARK UI Preview](https://img.shields.io/badge/UI-Next.js%2014%20App%20Router-black?logo=next.js) ![Backend](https://img.shields.io/badge/AI_Engine-FastAPI%20%2B%20LlamaIndex-009688?logo=fastapi) ![GCP](https://img.shields.io/badge/Infra-Google%20Cloud%20Run-4285F4?logo=google-cloud)

An enterprise-grade, agentic AI Campus Assistant built for B. P. Poddar Institute of Management & Technology. Restricted exclusively to `@bppimt.ac.in` users via a zero-trust OAuth boundary.

## 🌟 Key Features

* **Agentic Workflows:** Uses LlamaIndex 0.14 `ReActAgent` to autonomously select tools based on natural language queries.
* **Vector Search RAG:** Semantically searches BPPIMT syllabuses, fee structures, notices, and rules via BigQuery Vector Store.
* **Dynamic Calendar Export:** Instantly parses timetable data and generates downloadable `.ics` calendar files.
* **Faculty Scheduler:** Checks professor availability and automatically drafts professional meeting request emails.
* **Real-time SSE Streaming:** Token-by-token streaming with live tool-execution pills ("Generating calendar...", "Checking schedule...").

## 🏗️ Architecture

```
bppimt-campus-assistant/
├── frontend/   — Next.js 14 App Router, NextAuth, Radix UI, Tailwind CSS
├── backend/    — FastAPI, LlamaIndex, Google Vertex AI (Gemini 2.5 Flash)
├── cloudbuild.yaml — Declarative CI/CD Pipeline for Google Cloud Run
```

### Authentication Architecture (Zero-Trust)

| Layer | Mechanism |
|---|---|
| 1 — OAuth Hint | `hd: "bppimt.ac.in"` enforced on Google account picker |
| 2 — JWT Validation | NextAuth secures frontend; FastAPI cryptographically verifies raw Google ID Bearer tokens |
| 3 — Domain Assertion | Regex + `email_verified` flag → HTTP 403 for non-BPPIMT tokens |

---

## 🚀 Deployment (Google Cloud Run)

The application is fully containerized with highly-optimized, multi-stage Dockerfiles (`frontend.Dockerfile` leverages Next.js `standalone` mode; `backend.Dockerfile` uses `python:3.11-slim`). Containers drop root privileges and run as dedicated unprivileged users for security.

### 1. Initialize Infrastructure
Enable necessary APIs:
```bash
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com aiplatform.googleapis.com bigquery.googleapis.com
```

### 2. Set Up Service Account
Create a least-privilege service account specifically for Cloud Run:
```bash
gcloud iam service-accounts create bppimt-run-sa --display-name="BPPIMT Run SA"
```
Bind the required roles (Note: Use exact Agent Platform User role):
* `roles/aiplatform.user` (For Vertex AI / Gemini)
* `roles/bigquery.dataViewer` & `roles/bigquery.jobUser` (For Vector Search)
* `roles/secretmanager.secretAccessor` (For NextAuth secrets)

### 3. Deploy via Cloud Build
Trigger the zero-downtime rolling update pipeline directly from the root directory:
```bash
gcloud builds submit --config cloudbuild.yaml .
```
*Cloud Build will automatically compile the images, deploy the backend, capture its URL, and inject it securely into the frontend deployment.*

---

## 💻 Local Development Setup

### Prerequisites
- Node.js 20+
- Python 3.11+
- A GCP Project with Application Default Credentials (`gcloud auth application-default login`)

### Run Individually (development)

**Frontend:**
```bash
cd frontend
cp .env.local.example .env.local  # Populate OAuth Client ID/Secret
npm install
npm run dev
```

**Backend:**
```bash
cd backend
cp ../.env.example .env           # Populate BigQuery Dataset/Table config
pip install -e ".[dev]"
uvicorn main:app --reload --port 8000
```
