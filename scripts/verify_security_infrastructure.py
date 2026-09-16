"""
Comprehensive Automated Verification Suite for Problem 16 — Security Infrastructure

Tests:
1. Startup Secrets validation (missing/short SESSION_TOKEN_SECRET, missing GROQ_API_KEY)
2. CORS & Security headers middleware (nosniff, DENY, strict-origin-when-cross-origin, methods, headers)
3. Admin router-level guard (non-admin receives 403 Forbidden)
4. Auth rate limiting with slowapi (10/min on /auth/login, 3/min on /auth/signup)
5. File upload security (path traversal sanitization, null bytes, magic byte verification, 10MB cap)
6. OAuth code exchange & re-use prevention (second exchange strictly returns 400 Bad Request)
7. Audit log append-only immutability trigger (UPDATE and DELETE blocked by PostgreSQL trigger)
"""
import io
import os
import subprocess
import sys
import uuid
from starlette.testclient import TestClient

# Ensure app is importable
sys.path.insert(0, os.path.abspath("."))

from app.main import app
from app.services.auth import create_session_token, create_oauth_exchange_code
from app.services.document_persistence import sanitize_filename
from app.database import SessionLocal
from app.models.tenant import Tenant
from app.models.user import User
from app.models.project import Project
from app.models.stage import Stage
from app.models.team import Team
from sqlalchemy import text


def run_tests():
    print("================================================================================")
    print("STARTING SECURITY INFRASTRUCTURE VERIFICATION (Plan Steps 1–8)")
    print("================================================================================")

    python_exe = sys.executable

    # ----------------------------------------------------
    # TEST 1: Startup Secrets Validation (Step 2)
    # ----------------------------------------------------
    print("\n[TEST 1] Verifying Startup Secrets Validation (app/config.py)...")

    # Case 1A: SESSION_TOKEN_SECRET is missing/empty
    env_empty_secret = os.environ.copy()
    env_empty_secret["SESSION_TOKEN_SECRET"] = ""
    env_empty_secret["GROQ_API_KEY"] = "dummy_groq_key"
    res_1a = subprocess.run(
        [python_exe, "-c", "import app.config"],
        capture_output=True, text=True, env=env_empty_secret
    )
    assert res_1a.returncode != 0, "Expected failure when SESSION_TOKEN_SECRET is empty"
    assert "SESSION_TOKEN_SECRET must be set and at least 32 characters" in (res_1a.stderr + res_1a.stdout)
    print("  -> Case 1A: Empty SESSION_TOKEN_SECRET correctly raised RuntimeError (< 32 chars)")

    # Case 1B: SESSION_TOKEN_SECRET is shorter than 32 characters
    env_short_secret = os.environ.copy()
    env_short_secret["SESSION_TOKEN_SECRET"] = "too_short_secret_only_24_c"
    env_short_secret["GROQ_API_KEY"] = "dummy_groq_key"
    res_1b = subprocess.run(
        [python_exe, "-c", "import app.config"],
        capture_output=True, text=True, env=env_short_secret
    )
    assert res_1b.returncode != 0, "Expected failure when SESSION_TOKEN_SECRET < 32 characters"
    assert "SESSION_TOKEN_SECRET must be set and at least 32 characters" in (res_1b.stderr + res_1b.stdout)
    print("  -> Case 1B: Short SESSION_TOKEN_SECRET (24 chars) correctly raised RuntimeError (< 32 chars)")

    # Case 1C: GROQ_API_KEY is missing/empty
    env_empty_groq = os.environ.copy()
    env_empty_groq["SESSION_TOKEN_SECRET"] = "a" * 32
    env_empty_groq["GROQ_API_KEY"] = ""
    res_1c = subprocess.run(
        [python_exe, "-c", "import app.config"],
        capture_output=True, text=True, env=env_empty_groq
    )
    assert res_1c.returncode != 0, "Expected failure when GROQ_API_KEY is empty"
    assert "GROQ_API_KEY must be set" in (res_1c.stderr + res_1c.stdout)
    print("  -> Case 1C: Missing GROQ_API_KEY correctly raised RuntimeError")

    # Case 1D: Valid secrets pass cleanly
    env_valid = os.environ.copy()
    env_valid["SESSION_TOKEN_SECRET"] = "a" * 32
    env_valid["GROQ_API_KEY"] = "valid_test_groq_key"
    res_1d = subprocess.run(
        [python_exe, "-c", "import app.config"],
        capture_output=True, text=True, env=env_valid
    )
    assert res_1d.returncode == 0, f"Valid secrets failed: {res_1d.stderr}"
    print("  -> Case 1D: Valid secrets loaded successfully (exit code 0)")

    # ----------------------------------------------------
    # TEST 2: CORS & Security Headers (Step 1)
    # ----------------------------------------------------
    print("\n[TEST 2] Verifying Security Headers & CORS (app/main.py)...")
    client = TestClient(app)

    res = client.get("/health")
    assert res.status_code == 200
    assert res.headers.get("X-Content-Type-Options") == "nosniff", f"Missing nosniff: {res.headers}"
    assert res.headers.get("X-Frame-Options") == "DENY", f"Missing DENY: {res.headers}"
    assert res.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin", f"Missing Referrer-Policy: {res.headers}"
    print("  -> Security headers verified: X-Content-Type-Options: nosniff, X-Frame-Options: DENY, Referrer-Policy: strict-origin-when-cross-origin")

    cors_res = client.options("/health", headers={
        "Origin": "http://localhost:5173",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "Authorization, Content-Type",
    })
    assert cors_res.headers.get("access-control-allow-origin") == "http://localhost:5173"
    allowed_methods = cors_res.headers.get("access-control-allow-methods", "")
    for m in ["GET", "POST", "PUT", "DELETE", "PATCH"]:
        assert m in allowed_methods, f"Method {m} missing from CORS allow-methods: {allowed_methods}"
    allowed_headers = cors_res.headers.get("access-control-allow-headers", "")
    assert "Authorization" in allowed_headers or "authorization" in allowed_headers.lower()
    assert "Content-Type" in allowed_headers or "content-type" in allowed_headers.lower()
    print("  -> CORS verified: origin http://localhost:5173, methods [GET, POST, PUT, DELETE, PATCH], headers [Authorization, Content-Type]")

    # ----------------------------------------------------
    # TEST 3: Admin Router-Level Guard (Step 3)
    # ----------------------------------------------------
    print("\n[TEST 3] Verifying Admin Router Guard (app/routers/admin.py)...")
    db = SessionLocal()
    try:
        tenant = db.query(Tenant).first()
        if not tenant:
            tenant = Tenant(name="Test Org")
            db.add(tenant)
            db.commit()
            db.refresh(tenant)

        non_admin_user = User(
            email=f"nonadmin_{uuid.uuid4().hex[:8]}@docflow.test",
            tenant_id=tenant.tenant_id,
            is_org_admin=False,
            password_hash=None,
        )
        db.add(non_admin_user)
        db.commit()
        db.refresh(non_admin_user)

        token = create_session_token(str(non_admin_user.user_id))
        admin_res = client.get("/admin/users", headers={"Authorization": f"Bearer {token}"})
        assert admin_res.status_code == 403, f"Expected 403, got {admin_res.status_code}: {admin_res.text}"
        assert "Organization admin access required" in admin_res.text
        print("  -> Non-admin request to /admin/users strictly returned 403 Forbidden ('Organization admin access required')")
    finally:
        db.close()

    # ----------------------------------------------------
    # TEST 4: Rate Limiting on Auth Endpoints (Step 7)
    # ----------------------------------------------------
    print("\n[TEST 4] Verifying Rate Limiting (10/min on /auth/login, 3/min on /auth/signup)...")

    # A: Login (10/minute) -> 11 requests: 1–10 processed, 11th returns 429
    login_payload = {"email": "ratelimit_login@docflow.test", "password": "WrongPassword123!"}
    login_statuses = []
    for i in range(11):
        r = client.post("/auth/login", json=login_payload)
        login_statuses.append(r.status_code)

    print(f"  -> Login responses (11 attempts): {login_statuses}")
    assert login_statuses[:10] == [401] * 10, f"Expected first 10 requests to be processed (401), got: {login_statuses[:10]}"
    assert login_statuses[10] == 429, f"11th request must return 429 Too Many Requests, got: {login_statuses[10]}"
    print("  -> /auth/login rate limit verified: requests 1–10 processed (401), 11th returned 429")

    # B: Signup (3/minute) -> 4 requests: 1–3 processed, 4th returns 429
    signup_payload = {"email": "ratelimit_signup@docflow.test", "password": "SamplePassword123!"}
    signup_statuses = []
    for i in range(4):
        r = client.post("/auth/signup", json=signup_payload)
        signup_statuses.append(r.status_code)

    print(f"  -> Signup responses (4 attempts): {signup_statuses}")
    assert all(s in (201, 409, 422) for s in signup_statuses[:3]), f"Expected first 3 requests to be processed, got: {signup_statuses[:3]}"
    assert signup_statuses[3] == 429, f"4th request must return 429 Too Many Requests, got: {signup_statuses[3]}"
    print("  -> /auth/signup rate limit verified: requests 1–3 processed, 4th returned 429")

    # ----------------------------------------------------
    # TEST 5: File Upload Security (Step 4)
    # ----------------------------------------------------
    print("\n[TEST 5] Verifying File Upload Security (sanitization, magic bytes, 10MB cap)...")

    # Path traversal sanitization
    assert sanitize_filename("../../etc/passwd.pdf") == "passwd.pdf", "Failed path traversal stripping"
    assert sanitize_filename("folder/subfolder/document.pdf") == "document.pdf", "Failed directory prefix stripping"
    print("  -> Path traversal sanitization verified: '../../etc/passwd.pdf' -> 'passwd.pdf'")

    # Null-byte stripping
    assert sanitize_filename("sample\x00file.pdf") == "samplefile.pdf", "Failed null-byte stripping"
    print("  -> Null-byte injection sanitization verified: 'sample\\x00file.pdf' -> 'samplefile.pdf'")

    # Non-alphanumeric/invalid filename raises ValueError
    try:
        sanitize_filename("/?$*&#")
        raise AssertionError("Expected ValueError for all-invalid filename")
    except ValueError:
        print("  -> All-invalid filename properly raised ValueError")

    # Extension spoofing (fake PDF) & 10MB cap via endpoint
    db = SessionLocal()
    admin_user_id = None
    try:
        tenant = db.query(Tenant).first()
        if not tenant:
            tenant = Tenant(name="Upload Test Org")
            db.add(tenant)
            db.commit()
            db.refresh(tenant)

        db.info["tenant_id"] = str(tenant.tenant_id)
        db.execute(text("SET LOCAL app.current_tenant_id = :tid"), {"tid": str(tenant.tenant_id)})

        project = db.query(Project).filter(Project.tenant_id == tenant.tenant_id).first()
        if not project:
            project = Project(name="Upload Test Project", tenant_id=tenant.tenant_id)
            db.add(project)
            db.commit()
            db.refresh(project)
        stage = db.query(Stage).filter(Stage.project_id == project.project_id).first()
        if not stage:
            stage = Stage(name="Intake", project_id=project.project_id, order_index=1)
            db.add(stage)
            db.commit()
            db.refresh(stage)
        team = db.query(Team).filter(Team.project_id == project.project_id).first()
        if not team:
            team = Team(name="Engineering", project_id=project.project_id)
            db.add(team)
            db.commit()
            db.refresh(team)

        admin_user = User(
            email=f"uploader_{uuid.uuid4().hex[:8]}@docflow.test",
            tenant_id=tenant.tenant_id,
            is_org_admin=True,
        )
        db.add(admin_user)
        db.commit()
        db.refresh(admin_user)
        admin_user_id = str(admin_user.user_id)
        uploader_token = create_session_token(admin_user_id)

        # Spoofed file (text bytes disguised as PDF)
        fake_pdf = io.BytesIO(b"Hello world, I am actually a plain text file, not a PDF!")
        upload_res = client.post(
            "/documents/upload-file",
            files={"file": ("malicious.pdf", fake_pdf, "application/pdf")},
            data={"stage_id": str(stage.stage_id), "team_id": str(team.team_id)},
            headers={"Authorization": f"Bearer {uploader_token}"},
        )
        assert upload_res.status_code == 415, f"Expected 415 for spoofed extension, got {upload_res.status_code}: {upload_res.text}"
        print("  -> Fake PDF spoofing rejected with 415 Unsupported Media Type")

        # Oversized file (> 10MB)
        oversized = io.BytesIO(b"0" * (10 * 1024 * 1024 + 1024))
        size_res = client.post(
            "/documents/upload-file",
            files={"file": ("large.txt", oversized, "text/plain")},
            data={"stage_id": str(stage.stage_id), "team_id": str(team.team_id)},
            headers={"Authorization": f"Bearer {uploader_token}"},
        )
        assert size_res.status_code == 413, f"Expected 413 for oversized file, got {size_res.status_code}: {size_res.text}"
        print("  -> 10MB+ file upload rejected with 413 Payload Too Large (via file.read(MAX_UPLOAD_BYTES + 1))")
    finally:
        db.close()

    # ----------------------------------------------------
    # TEST 6: OAuth Authorization Code Flow (Step 8)
    # ----------------------------------------------------
    print("\n[TEST 6] Verifying OAuth Code Exchange & Re-Use Prevention (Step 8)...")
    real_token = create_session_token(admin_user_id)
    exchange_code = create_oauth_exchange_code(real_token)

    # First exchange: must succeed
    ex1 = client.post("/auth/exchange", json={"code": exchange_code})
    assert ex1.status_code == 200, f"Expected 200, got {ex1.status_code}: {ex1.text}"
    received_token = ex1.json().get("access_token")
    assert received_token == real_token, "Exchanged token did not match original token"
    print("  -> First exchange succeeded with 200 OK and valid session token")

    # Second exchange with same code: MUST strictly return 400 Bad Request
    ex2 = client.post("/auth/exchange", json={"code": exchange_code})
    assert ex2.status_code == 400, f"Expected strict 400 on reused code, got {ex2.status_code}: {ex2.text}"
    assert ex2.json().get("detail") == "Invalid or expired exchange code"
    print("  -> Re-use attempt strictly returned 400 Bad Request with 'Invalid or expired exchange code'")

    # ----------------------------------------------------
    # TEST 7: Audit Log Immutability Trigger (Step 6)
    # ----------------------------------------------------
    print("\n[TEST 7] Verifying Audit Log Append-Only Immutability Trigger (Step 6)...")
    db = SessionLocal()
    try:
        try:
            db.execute(text("UPDATE audit_log SET action = 'tampered' WHERE true;"))
            db.commit()
            raise AssertionError("UPDATE audit_log should have been rejected by trigger!")
        except Exception as exc:
            db.rollback()
            assert "audit_log table is append-only" in str(exc), f"Unexpected exception: {exc}"
            print("  -> PostgreSQL trigger successfully blocked UPDATE on audit_log ('audit_log table is append-only')")

        try:
            db.execute(text("DELETE FROM audit_log WHERE true;"))
            db.commit()
            raise AssertionError("DELETE FROM audit_log should have been rejected by trigger!")
        except Exception as exc:
            db.rollback()
            assert "audit_log table is append-only" in str(exc), f"Unexpected exception: {exc}"
            print("  -> PostgreSQL trigger successfully blocked DELETE on audit_log ('audit_log table is append-only')")
    finally:
        db.close()

    print("\n================================================================================")
    print("ALL SECURITY INFRASTRUCTURE TESTS (1–7) PASSED STRICTLY!")
    print("================================================================================")


if __name__ == "__main__":
    run_tests()
