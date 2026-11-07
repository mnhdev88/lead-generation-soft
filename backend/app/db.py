import logging

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import DATABASE_URL

log = logging.getLogger("db")

connect_args = {"check_same_thread": False, "timeout": 30} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def _add_missing_columns() -> None:
    """Tiny auto-migration: when new columns are added to models.py, add them to the
    existing database too (create_all only creates missing *tables*).
    New columns must be nullable or have a server_default."""
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                ddl = f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {col.type.compile(engine.dialect)}'
                if col.server_default is not None:
                    ddl += f" DEFAULT {col.server_default.arg.text}"
                log.info("Migrating: %s", ddl)
                conn.execute(text(ddl))


def _drop_doubled_list_items() -> None:
    """Older versions could put the same company or person in a list twice. Keep the first
    copy, so the unique indexes on list_items can be created."""
    with engine.begin() as conn:
        if not inspect(conn).has_table("list_items"):
            return
        for column in ("company_id", "contact_id"):
            result = conn.execute(text(
                f"DELETE FROM list_items WHERE {column} IS NOT NULL AND id NOT IN ("
                f"SELECT MIN(id) FROM list_items WHERE {column} IS NOT NULL GROUP BY list_id, {column})"
            ))
            if result.rowcount:
                log.info("Removed %s doubled list entries (%s)", result.rowcount, column)


def _add_missing_indexes() -> None:
    """Indexes of columns added by _add_missing_columns (create_all skips existing tables)."""
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            for index in table.indexes:
                index.create(conn, checkfirst=True)


def init_db() -> None:
    from . import models  # noqa: F401  (registers tables)

    Base.metadata.create_all(engine)
    _add_missing_columns()
    _drop_doubled_list_items()
    _add_missing_indexes()


def get_session():
    """FastAPI dependency: one DB session per request."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
