"""
AgentLine — Database
Async PostgreSQL connection pool using asyncpg directly.
We use raw asyncpg for maximum performance with direct SQL queries.
"""

import asyncpg
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from agentline.config import settings

logger = logging.getLogger(__name__)

# Module-level pool reference
_pool: asyncpg.Pool | None = None


async def init_db():
    """Initialize the connection pool. Call once at app startup."""
    global _pool
    dsn = settings.db_dsn
    try:
        _pool = await asyncpg.create_pool(
            dsn=dsn,
            min_size=2,
            max_size=10,
            command_timeout=30,
        )
        # Test the connection
        async with _pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        logger.info("Database connected successfully")

        # Auto-create call_responses table (required for /speak → wait loop relay)
        async with _pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS call_responses (
                    id SERIAL PRIMARY KEY,
                    call_id TEXT REFERENCES calls(id) ON DELETE CASCADE,
                    response_text TEXT NOT NULL,
                    spoken BOOLEAN DEFAULT false,
                    created_at TIMESTAMPTZ DEFAULT now()
                )
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_call_responses_pending
                ON call_responses (call_id, spoken)
                WHERE spoken = false
            """)
            logger.info("call_responses table verified")

            # Open-source installs do not keep a hosted balance or Supabase user id.
            await conn.execute("DROP INDEX IF EXISTS idx_billing_ledger_account")
            await conn.execute("DROP INDEX IF EXISTS idx_billing_ledger_type")
            await conn.execute("DROP TABLE IF EXISTS billing_ledger")
            await conn.execute("ALTER TABLE accounts DROP COLUMN IF EXISTS balance")
            await conn.execute("ALTER TABLE accounts DROP COLUMN IF EXISTS supabase_user_id")
            await conn.execute("DROP INDEX IF EXISTS idx_accounts_supabase")
            await conn.execute(
                "ALTER TABLE phone_numbers ADD COLUMN IF NOT EXISTS provider TEXT"
            )
            await conn.execute(
                "ALTER TABLE calls ADD COLUMN IF NOT EXISTS provider TEXT"
            )
            await conn.execute(
                "ALTER TABLE agents ADD COLUMN IF NOT EXISTS voice_runtime TEXT"
            )
            logger.info("Open-source schema verified")

            # Auto-add initial_greeting column to calls (per-call greeting override)
            await conn.execute("""
                ALTER TABLE calls
                    ADD COLUMN IF NOT EXISTS initial_greeting TEXT
            """)
            logger.info("calls.initial_greeting column verified")
    except Exception as e:
        logger.error("Database connection failed: %s", e)
        logger.warning("Server starting WITHOUT database — fix DATABASE_URL in .env")
        _pool = None


async def close_db():
    """Close the connection pool. Call at app shutdown."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


async def get_db() -> asyncpg.Connection:
    """
    FastAPI dependency that yields a connection from the pool.
    Usage: db = Depends(get_db)
    """
    if _pool is None:
        raise RuntimeError("Database not available. Check DATABASE_URL in .env")
    async with _pool.acquire() as conn:
        yield conn


@asynccontextmanager
async def get_db_conn() -> AsyncGenerator[asyncpg.Connection, None]:
    """
    Context manager for getting a DB connection outside of FastAPI routes.
    Usage: async with get_db_conn() as db: ...
    """
    if _pool is None:
        raise RuntimeError("Database not available. Check DATABASE_URL in .env")
    async with _pool.acquire() as conn:
        yield conn
