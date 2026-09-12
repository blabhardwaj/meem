import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL: str = os.getenv("DATABASE_URL")
GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL: str = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
MAX_UPLOAD_SIZE_BYTES: int = int(os.getenv("MAX_UPLOAD_SIZE_BYTES", 10 * 1024 * 1024))

ALLOWED_MIME_TYPES: set[str] = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # .docx
    "text/plain",
    "text/markdown",
}
ALLOWED_EXTENSIONS: set[str] = {".pdf", ".docx", ".txt", ".md"}

# --- Phase 2: Authentication ---------------------------------------------------
# Shared secret used to HMAC-sign session tokens and OAuth state. Required for
# any auth operation; validated lazily in app/services/auth.py (not raised here,
# so CLI/agent code that never touches auth can still run without it set).
SESSION_TOKEN_SECRET: str = os.getenv("SESSION_TOKEN_SECRET", "")
SESSION_TOKEN_TTL_SECONDS: int = int(os.getenv("SESSION_TOKEN_TTL_SECONDS", 8 * 60 * 60))
PASSWORD_HASH_ITERATIONS: int = int(os.getenv("PASSWORD_HASH_ITERATIONS", 310_000))
DEFAULT_NEW_USER_PASSWORD: str = os.getenv("DEFAULT_NEW_USER_PASSWORD", "DemoPassword123!")

# Google OAuth2 — leave blank to disable Google sign-in (endpoints then 503).
GOOGLE_CLIENT_ID: str = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET: str = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_OAUTH_REDIRECT_URI: str = os.getenv(
    "GOOGLE_OAUTH_REDIRECT_URI", "http://localhost:8000/auth/google/callback"
)

# Where the SPA runs — the Google callback 302-redirects here with ?auth_token=.
FRONTEND_URL: str = os.getenv("FRONTEND_URL", "http://localhost:5173")

# tenants.tenant_id (UUID) that new signups / first-time Google users join.
# No sensible default in a multi-tenant system — must be set for signup to work.
DEFAULT_SIGNUP_TENANT_ID: str = os.getenv("DEFAULT_SIGNUP_TENANT_ID", "")

# --- Phase A: RAG / vector store (Qdrant) -------------------------------------
# QDRANT_URL blank  -> embedded local mode (on-disk at QDRANT_LOCAL_PATH), no
#                      server, no key. Fine for dev; supports named + sparse
#                      vectors and payload indexes.
# QDRANT_URL set    -> connect to that Qdrant (local Docker on :6333, or a
#                      Qdrant Cloud cluster URL). QDRANT_API_KEY needed for
#                      Qdrant Cloud, not for a bare local server.
QDRANT_URL: str = os.getenv("QDRANT_URL", "")
QDRANT_API_KEY: str = os.getenv("QDRANT_API_KEY", "")
QDRANT_LOCAL_PATH: str = os.getenv("QDRANT_LOCAL_PATH", "qdrant_storage")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL not set — check your .env file")