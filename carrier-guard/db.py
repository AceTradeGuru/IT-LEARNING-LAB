"""
CarrierGuard — database access.

A pooled Postgres connection, and a migration runner.

The pool matters more than it looks: a check runs half a dozen queries, and
opening a fresh Postgres connection per request costs more than all of them
combined. Managed Postgres also caps connections far lower than most people
expect, so an unpooled app falls over under load that the database itself
would not have noticed.

Callers receive a connection and pass it down (`run_check(conn, ...)`), which
keeps transaction boundaries explicit and lets the risk engine be tested
against a rolled-back transaction instead of a live database.
"""

import threading
from contextlib import contextmanager
from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from config import load

cfg = load()
MIGRATIONS_DIR = Path(__file__).parent / "migrations"

# open=False: the pool connects on first use rather than at import time, so a
# database that is briefly unreachable doesn't prevent the process from booting.
_pool = ConnectionPool(
    conninfo=cfg.DATABASE_URL,
    min_size=cfg.DB_POOL_MIN,
    max_size=cfg.DB_POOL_MAX,
    kwargs={"row_factory": dict_row},
    open=False,
)


_opened = False
_open_lock = threading.Lock()


def _ensure_open():
    """
    Open the pool on first use.

    Paired with open=False above: the process can boot while the database is
    still coming up (common on a fresh deploy, where app and Postgres start
    together), and the first request pays the connection cost instead of the
    container failing its health check and getting killed.
    """
    global _opened
    if _opened:
        return
    with _open_lock:
        if not _opened:
            _pool.open(wait=True, timeout=10)
            _opened = True


@contextmanager
def connection():
    """
    Yield a pooled connection inside a transaction.

    Commits on clean exit, rolls back on exception. Callers should not commit
    themselves — letting the context manager own the boundary is what makes a
    failed check leave no half-written signals behind.
    """
    _ensure_open()
    with _pool.connection() as conn:
        yield conn


def close_pool():
    global _opened
    if _opened:
        _pool.close()
        _opened = False


# ---------------------------------------------------------------- migrations

def _applied(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    TEXT PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )""")
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {r["version"] for r in rows}


# Arbitrary but fixed: every process that might migrate must agree on it.
MIGRATION_LOCK_KEY = 8_274_119_053


def migrate(verbose=True):
    """
    Apply every unapplied migration in filename order, each in its own
    transaction. Postgres runs DDL transactionally, so a migration that fails
    halfway leaves the schema untouched rather than half-built.

    Guarded by a session-level advisory lock. A rolling deploy starts several
    gunicorn workers at once, and without the lock they would each read the same
    empty schema_migrations table and race to apply the same CREATE TABLE — one
    wins, the rest crash. Losers block here, then find nothing pending.
    """
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        raise RuntimeError(f"No migrations found in {MIGRATIONS_DIR}")

    applied = []
    with connection() as lock_conn:
        lock_conn.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK_KEY,))
        try:
            with connection() as conn:
                done = _applied(conn)

            pending = [f for f in files if f.stem not in done]
            if not pending:
                if verbose:
                    print(f"Schema up to date ({len(done)} migrations applied).")
                return []

            for path in pending:
                with connection() as conn:
                    conn.execute(path.read_text())
                    conn.execute(
                        "INSERT INTO schema_migrations (version) VALUES (%s)",
                        (path.stem,))
                applied.append(path.stem)
                if verbose:
                    print(f"  applied {path.name}")
        finally:
            # Session-level, so it outlives the transaction: the pooled
            # connection would carry the lock back into the pool if we skipped this.
            lock_conn.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK_KEY,))

    return applied


def reset(verbose=True):
    """Drop everything and rebuild. Development only — refuses in production."""
    if cfg.IS_PRODUCTION:
        raise RuntimeError("reset() is disabled in production.")
    with connection() as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    if verbose:
        print("Schema dropped.")
    migrate(verbose=verbose)


if __name__ == "__main__":
    import sys

    if "--reset" in sys.argv:
        reset()
    else:
        migrate()
    close_pool()
