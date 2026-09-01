"""SQLAlchemy base and session handling."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from themis.config import load_settings


class Base(DeclarativeBase):
    """Declarative base for every THEMIS table."""


# JSONB on Postgres, plain JSON everywhere else. The SQLite path exists so the tests
# run without a database container; production is Postgres.
JsonType = JSON().with_variant(JSONB(), "postgresql")

_engine: Engine | None = None
_factory: sessionmaker[Session] | None = None


def get_engine(url: str | None = None) -> Engine:
    global _engine, _factory
    target = url or load_settings().database_url
    if _engine is None or str(_engine.url) != target:
        # pool_pre_ping: workers are long-lived and idle between claims, so a
        # connection dropped by the server must not surface as a failed review.
        _engine = create_engine(target, pool_pre_ping=True, future=True)
        _factory = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


@contextmanager
def session_scope(url: str | None = None) -> Iterator[Session]:
    """A transactional session. Commits on success, rolls back on failure."""
    get_engine(url)
    assert _factory is not None
    session = _factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
