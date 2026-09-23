"""
Change 4 (PRODUCTION_READINESS_PLAN.md): per-tenant AI usage gate.

Uses an in-memory SQLite engine rather than the shared Supabase dev DB —
verified that check_and_consume_ai_usage's ON CONFLICT DO NOTHING + atomic
UPDATE...RETURNING both compile and behave correctly against SQLite too,
and this avoids competing for the already-capped live connection pool.
"""
import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models.ai_usage import AIUsage
from app.services.ai_usage import DEFAULT_CALLS_LIMIT, AIUsageLimitExceededError, check_and_consume_ai_usage


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[AIUsage.__table__])
    session = Session(engine)
    yield session
    session.close()


def _get_row(db, tenant_id):
    return db.execute(select(AIUsage).where(AIUsage.tenant_id == tenant_id)).scalar_one()


def test_first_call_lazily_creates_row_and_increments(db):
    tenant_id = uuid.uuid4()
    check_and_consume_ai_usage(db, tenant_id)
    db.commit()

    row = _get_row(db, tenant_id)
    assert row.calls_used == 1
    assert row.calls_limit == DEFAULT_CALLS_LIMIT


def test_repeated_calls_increment_monotonically(db):
    tenant_id = uuid.uuid4()
    for i in range(1, 6):
        check_and_consume_ai_usage(db, tenant_id)
        db.commit()
        assert _get_row(db, tenant_id).calls_used == i


def test_exhausted_limit_raises_and_stops_incrementing(db):
    tenant_id = uuid.uuid4()
    check_and_consume_ai_usage(db, tenant_id)  # creates the row
    db.commit()
    row = _get_row(db, tenant_id)
    row.calls_limit = 2
    db.commit()

    check_and_consume_ai_usage(db, tenant_id)  # used=2, at limit
    db.commit()
    assert _get_row(db, tenant_id).calls_used == 2

    with pytest.raises(AIUsageLimitExceededError) as exc_info:
        check_and_consume_ai_usage(db, tenant_id)
    db.rollback()  # the raising statement's own (uncommitted) work rolls back
    assert exc_info.value.tenant_id == tenant_id

    # Still exactly 2 — the rejected attempt never incremented the counter.
    assert _get_row(db, tenant_id).calls_used == 2

    # Stays rejected on further attempts.
    with pytest.raises(AIUsageLimitExceededError):
        check_and_consume_ai_usage(db, tenant_id)
    db.rollback()


def test_usage_is_isolated_per_tenant(db):
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()

    check_and_consume_ai_usage(db, tenant_a)
    db.commit()
    row_a = _get_row(db, tenant_a)
    row_a.calls_limit = 1
    db.commit()

    with pytest.raises(AIUsageLimitExceededError):
        check_and_consume_ai_usage(db, tenant_a)
    db.rollback()

    # Tenant B is completely unaffected by tenant A being exhausted.
    check_and_consume_ai_usage(db, tenant_b)
    db.commit()
    assert _get_row(db, tenant_b).calls_used == 1
