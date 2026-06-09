# BPPIMT Campus Resource Assistant

## Overview
Enterprise-grade, RAG-based Campus Resource Chatbot restricted exclusively to `@bppimt.ac.in` users, built on a Next.js + FastAPI monorepo.

## Monorepo Structure
```
bppimt-campus-assistant/
├── frontend/   — Next.js 14 App Router (TypeScript, Tailwind, shadcn/ui)
└── backend/    — FastAPI + LlamaIndex RAG service (Python 3.11)
```

## Authentication Architecture (Zero-Trust)

| Layer | Location | Mechanism |
|---|---|---|
| 1 — OAuth Hint | `[...nextauth]/route.ts` | `hd: "bppimt.ac.in"` on Google account picker |
| 2 — Profile Validation | NextAuth `signIn` callback | `profile.hd` + email suffix assertion → returns `false` to abort |
| 3 — JWT Token Relay | NextAuth `jwt` callback | Raw Google ID token captured and surfaced on session |
| 4 — Edge Guard | `middleware.ts` | NextAuth `withAuth` blocks unauthenticated route access at the CDN edge |
| 5 — Backend Token Verify | `dependencies/auth.py` | `google.oauth2.id_token.verify_oauth2_token` → cryptographic signature + audience + expiry |
| 6 — Domain Assertion | `dependencies/auth.py` | Regex + `email_verified` flag → HTTP 403 for non-BPPIMT tokens |

## Local Development Setup

### Prerequisites
- Node.js 20+
- Python 3.11+
- Docker Desktop
- A GCP Project with Vertex AI and BigQuery APIs enabled
- A Google OAuth 2.0 Client ID (Web Application type)

### Environment Variables

Copy and populate:
```bash
cp .env.example frontend/.env.local
cp .env.example backend/.env
```

Required values:
| Variable | Description |
|---|---|
| `GOOGLE_CLIENT_ID` | OAuth 2.0 client ID from GCP Console |
| `GOOGLE_CLIENT_SECRET` | OAuth 2.0 client secret |
| `NEXTAUTH_SECRET` | Run `openssl rand -hex 32` to generate |
| `GOOGLE_OAUTH_CLIENT_ID` | Same client ID — used by FastAPI to verify Bearer tokens |
| `GOOGLE_CLOUD_PROJECT` | GCP project ID |
| `BIGQUERY_DATASET` | BigQuery dataset name |
| `BIGQUERY_TABLE` | BigQuery vector index table name |

### Run with Docker Compose
```bash
docker compose up --build
```

### Run Individually (development)

**Frontend:**
```bash
cd frontend
npm install
npm run dev
```

**Backend:**
```bash
cd backend
pip install -e ".[dev]"
uvicorn main:app --reload --port 8000
```

### Run Backend Tests
```bash
cd backend
pytest tests/test_auth.py -v --cov=dependencies
```
