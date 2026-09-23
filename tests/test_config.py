"""
Change 5 (PRODUCTION_READINESS_PLAN.md): production must not silently fall
back to embedded/local Qdrant. app/config.py raises at IMPORT time, so each
case is exercised in a fresh subprocess — importing app.config once already
caches it in sys.modules for the rest of the test session, and module-level
validation code can't be re-run via importlib.reload without also
re-running (and re-raising on) every other startup check in that module.
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Minimum env needed to get PAST every other app/config.py startup check, so
# only the QDRANT_URL/ENVIRONMENT behavior under test is actually exercised.
BASE_ENV = {
    "DATABASE_URL": "postgresql+psycopg2://user:pass@localhost:5432/db",
    "SESSION_TOKEN_SECRET": "x" * 32,
    "GROQ_API_KEY": "test-key",
}


def _run_import(extra_env: dict) -> subprocess.CompletedProcess:
    env = {**os.environ, **BASE_ENV, **extra_env}
    return subprocess.run(
        [sys.executable, "-c", "import app.config"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_production_without_qdrant_url_raises():
    result = _run_import({"ENVIRONMENT": "production", "QDRANT_URL": ""})
    assert result.returncode != 0
    assert "QDRANT_URL is required when ENVIRONMENT=production" in result.stderr


def test_production_with_qdrant_url_succeeds():
    result = _run_import({"ENVIRONMENT": "production", "QDRANT_URL": "http://localhost:6333"})
    assert result.returncode == 0, result.stderr


def test_development_without_qdrant_url_still_succeeds():
    # Default/unset ENVIRONMENT must behave exactly as before this change —
    # embedded/local Qdrant fallback stays allowed in development.
    result = _run_import({"ENVIRONMENT": "development", "QDRANT_URL": ""})
    assert result.returncode == 0, result.stderr


def test_unset_environment_defaults_to_development_behavior():
    env = {**os.environ, **BASE_ENV, "QDRANT_URL": ""}
    env.pop("ENVIRONMENT", None)
    result = subprocess.run(
        [sys.executable, "-c", "import app.config"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
