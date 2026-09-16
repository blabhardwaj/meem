# Meem / DocFlow AI

DocFlow AI is a multi-tenant document workflow and document intelligence platform built around secure ingestion, team-aware access control, approval pipelines, and AI-assisted retrieval and review.

This repository contains the backend application, shared services, database models, AI agents, and a React/Vite frontend for the DocFlow experience.

## What this project does

At a high level, the system lets teams:

- upload and track documents by project and stage
- enforce tenant, project, team, and sensitivity-based access controls
- maintain document identity and version history
- route documents through review and approval workflows
- ask questions over stored project content using retrieval-augmented generation (RAG)
- interact with a document review/chat workflow and AI assistant tooling
- manage work across multiple projects and teams from a single app experience

The repo is designed around a document-centric workflow rather than a generic CRUD app: documents are first-class entities, workflow states matter, and access is enforced by policy and role context.

## Architecture

The codebase is split into a few major areas:

- Backend API: `app/`
  - FastAPI app entrypoint in `app/main.py`
  - routers for auth, documents, workflow, chat, projects, stages, teams, admin, and notifications
  - SQLAlchemy models for users, tenants, projects, documents, stages, workflow, audit, notifications, and chat data
  - services for upload, parsing, review, approval, access control, and AI workflows
- Database and migrations: `alembic/`, `app/models/`
  - PostgreSQL-backed schema with Alembic migrations
  - tenant isolation and workflow-aware data model
- AI / document intelligence: `app/services/`, `app/agents/`, `app/tools/`
  - RAG indexing and retrieval
  - document scoring / reform / scan logic
  - draft generation and revision support
  - query/search agent patterns
- Frontend: `frontend/`
  - React + Vite + Tailwind-inspired UI
  - browser-based project/workspace experience
- Demo data: `seed/`
  - scripts to populate a realistic tenant/project dataset for local demo/testing

## Core platform capabilities

### Document lifecycle

The platform models documents as project-scoped artifacts with workflow status and versioning. Key concepts include:

- tenants
- projects
- stages
- teams / memberships
- document versions
- workflow state transitions
- approvals and rejections

This is more than file storage: it includes review states, ownership, access boundaries, and staging logic that sits around a document's lifetime.

### Access control

The app uses a layered policy model that combines:

- tenant boundaries
- project membership
- team membership
- role context (`viewer`, `contributor`, `team_lead`, project/admin, org admin)
- sensitivity levels (`public`, `internal`, `confidential`)
- stage/team access restrictions

This is handled via access-control and authorization services in `app/services/` and enforced in the application layer.

### AI + RAG

The repo includes retrieval and indexing components for document search and question answering:

- chunking and embedding logic
- Qdrant integration for vector search
- retrieval scoring and reranking
- generation prompts and grounded answer assembly
- document search / chat capabilities

This supports a document Q&A experience grounded in project material instead of generic chat alone.

### Review and approval

The app includes review-related flows for:

- document submission to review
- approval / rejection decisions
- pending approval views
- audit tracking and notification hooks
- workflow-based review gates per stage

## Tech stack

### Backend

- Python 3
- FastAPI
- SQLAlchemy 2
- PostgreSQL
- Alembic
- Pydantic
- python-multipart
- slowapi
- Qdrant client
- FastEmbed

### Frontend

- React
- Vite
- React Router
- React Markdown
- Lucide icons

### Infra / local tooling

- `.env` based configuration
- local dev server startup via `uvicorn`
- demo seeding scripts for realistic tenant/project data
- optional Qdrant local storage path

## Repository layout

```text
.
├── .env.example
├── .gitignore
├── README.md
├── DEFERRED_ITEMS.md
├── MERGE_DECISIONS.md
├── requirements.txt
├── alembic/
│   ├── env.py
│   └── versions/
├── app/
│   ├── agents/
│   ├── api/
│   ├── models/
│   ├── routers/
│   ├── schemas/
│   ├── services/
│   ├── tools/
│   ├── workers/
│   ├── config.py
│   ├── database.py
│   ├── limiter.py
│   └── main.py
├── cli.py
├── docs/
│   └── demo/
├── frontend/
│   ├── src/
│   ├── package.json
│   └── .env.example
├── seed/
│   ├── README.md
│   └── ...
├── scripts/
├── tests/
├── sanity_check.py
├── standalone.py
└── ...
```

## Quick start

### Prerequisites

Before running the app locally, make sure you have:

- Python installed
- PostgreSQL running locally
- a database created for the app (for example `docflow_ai`)
- optional: Qdrant if you want vector/indexed RAG flows enabled
- Node.js/npm for the React frontend

### 1) Create environment variables

Copy the example env file and fill in the required values:

```bash
cp .env.example .env
```

The root `.env` should include at minimum:

- `DATABASE_URL`
- `SESSION_TOKEN_SECRET`
- `GROQ_API_KEY`
- `DEFAULT_SIGNUP_TENANT_ID`
- optional frontend settings such as `FRONTEND_URL`

See `.env.example` for the full template.

### 2) Install Python dependencies

```bash
python -m venv venv
# Windows
venv\Scripts\activate
# macOS / Linux
source venv/bin/activate

pip install -r requirements.txt
```

### 3) Apply database migrations

Create the database in PostgreSQL first, then run:

```bash
alembic upgrade head
```

If you are creating the schema from scratch, you may also generate a migration when needed:

```bash
alembic revision --autogenerate -m "Describe your schema change"
```

### 4) Run the backend

```bash
uvicorn app.main:app --reload --port 8000
```

Then open:

- API docs: `http://localhost:8000/docs`
- health check: `http://localhost:8000/health`

### 5) Run the frontend

```bash
cd frontend
npm install
cp .env.example .env
npm run dev
```

The frontend is usually served on:

- `http://localhost:5173`

## Demo / seed data

The repo includes a demo seed system intended to populate a realistic tenant/project set with users, teams, stages, and documents.

For more detail, see:

- `seed/README.md`

Typical demo commands include:

```bash
python -m seed.seed_demo
python -m seed.seed_demo --verify
python -m seed.seed_demo --reset
```

This helps exercise the access model and workflow behavior without manually constructing data in the database.

## API and app flow

A typical user journey in this app is:

1. sign in / sign up
2. select or create a project
3. create or view project workspace
4. upload a document into a stage
5. review content / routing / metadata
6. submit for approval if required
7. use AI chat / RAG tools to answer project questions
8. approve or reject workflow items

The app is designed around an integrated document operations experience, not just standalone file upload.

## Current status and project maturity

This repository is a substantial platform implementation with a real backend, database schema, frontend, AI services, and workflow logic. It is not a toy scaffold.

At the same time, the project documents explicitly list deferred and intentionally parked work. See:

- `DEFERRED_ITEMS.md`
- `MERGE_DECISIONS.md`

Examples include some future work such as deeper deployment tooling, some auth refinements, and parts of the broader AI/agent roadmap.

## Main design notes

- The system keeps document and project data multi-tenant and logically segmented.
- Access is treated as an application concern, not just a UI concern.
- Document review states and approval gates are first-class parts of the workflow.
- RAG and AI tooling are meant to support real work products, not just demo chat.
- The repo contains both product code and strategic decision documentation for the platform architecture.

## Useful references

- `app/main.py` — API application entrypoint and router registration
- `app/config.py` — environment/configuration requirements
- `app/models/` — core domain models
- `app/services/` — business logic and AI orchestration
- `app/routers/` — API surface
- `frontend/` — browser application
- `seed/` — demo seed environment
- `DEFERRED_ITEMS.md` — intentionally deferred work
- `MERGE_DECISIONS.md` — architectural decisions and tradeoffs

## License

The repository does not currently show a repository-level license file in the root snapshot. Check the project root for licensing details before using the code in production or redistributing it.

## Contributing

This repository looks best suited for local iterative development and feature experiments. For changes:

1. update the relevant model / service / router
2. add or update migrations when schema changes affect PostgreSQL
3. validate the backend and frontend locally
4. keep the architecture notes in sync with actual implementation

## Summary

Meem / DocFlow AI is a document-first platform for secure team workflows, approval processes, and AI-assisted knowledge retrieval. It blends a production-leaning backend, a frontend workspace, and a sophisticated domain model intended to support document operations inside a multi-tenant organization.

If you are opening the repo for the first time, start with:

- `app/main.py`
- `app/config.py`
- `app/models/`
- `app/services/`
- `seed/README.md`
- `frontend/`

Those are the best places to understand how the project is structured and how the system actually behaves.
