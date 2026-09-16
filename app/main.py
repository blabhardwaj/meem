import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from app.limiter import limiter

from app.config import FRONTEND_URL
from app.routers import (
    access_requests,
    activity,
    admin,
    agents,
    auth,
    chat,
    document_review,
    documents,
    notifications,
    projects,
    project_intelligence,
    stages,
    teams,
    workflow,
    workspace,
)

app = FastAPI(title="DocFlow AI", version="0.1.0")

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


_raw_origins = os.environ.get("ALLOWED_ORIGINS", f"{FRONTEND_URL},http://localhost:5173,http://127.0.0.1:5173")
ALLOWED_ORIGINS = [o.strip() for o in _raw_origins.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    allow_headers=["Authorization", "Content-Type"],
)

@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


app.include_router(auth.router)
app.include_router(documents.router)
app.include_router(document_review.router)
app.include_router(workflow.router)
app.include_router(workspace.router)
app.include_router(projects.router)
app.include_router(project_intelligence.router)
app.include_router(stages.router)
app.include_router(teams.router)
app.include_router(admin.router)
app.include_router(access_requests.router)
app.include_router(activity.router)
app.include_router(agents.router)
app.include_router(chat.router)
app.include_router(notifications.router)


@app.get("/health")
def health_check():
    return {"status": "ok"}
