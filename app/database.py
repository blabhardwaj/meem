from sqlalchemy import create_engine, event, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker, declarative_base

from app.config import DATABASE_URL

@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

connect_args = {"check_same_thread": False} if "sqlite" in DATABASE_URL else {}

# Supabase's session-mode pooler hard-caps this app to 15 concurrent
# connections TOTAL (FATAL EMAXCONNSESSION beyond that — not a queueable
# local condition, a hard rejection from the remote pooler). The old
# defaults (pool_size=5, max_overflow=10 => up to 15 local connections)
# left zero headroom: any burst of concurrent requests already at or near
# 15 open connections, plus a heartbeat/health-check/one-off script
# connection, tipped straight into that rejection. pool_size/max_overflow
# now leave real headroom below the ceiling, and pool_timeout make a
# request that arrives when we're at OUR limit wait for a connection to
# free up (queues, as QueuePool is meant to) instead of erroring out.
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=5,
    pool_timeout=15,
    connect_args=connect_args,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@event.listens_for(SessionLocal, "after_begin")
def set_tenant_id_on_begin(session, transaction, connection):
    tenant_id = session.info.get("tenant_id")
    if tenant_id:
        connection.execute(
            text("SET LOCAL app.current_tenant_id = :tid"),
            {"tid": str(tenant_id)},
        )


Base = declarative_base()



def get_db():
    """FastAPI dependency — yields a DB session, always closed after the request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
