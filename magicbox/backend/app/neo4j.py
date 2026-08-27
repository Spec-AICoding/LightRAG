"""Async, read-only Neo4j access for the magicbox backend.

neo4j 6.x driver notes: ``default_access_mode`` is a SESSION-level config
(it was removed from the driver constructor), so read-only enforcement
lives in :func:`get_session`.
"""

import logging

from neo4j import READ_ACCESS, AsyncDriver, AsyncGraphDatabase, AsyncSession

from .config import get_config

logger = logging.getLogger("magicbox")

_driver: AsyncDriver | None = None


def get_driver() -> AsyncDriver:
    """Return the shared async driver."""
    global _driver
    if _driver is None:
        cfg = get_config()
        _driver = AsyncGraphDatabase.driver(
            cfg.neo4j_uri,
            auth=(cfg.neo4j_username, cfg.neo4j_password),
        )
    return _driver


def get_session(**kwargs) -> AsyncSession:
    """Return a read-only session on the configured database.

    The service is query-only by design; enforcing READ_ACCESS at session
    creation turns any accidental write into an immediate error.
    """
    cfg = get_config()
    kwargs.setdefault("database", cfg.neo4j_database)
    kwargs.setdefault("default_access_mode", READ_ACCESS)
    return get_driver().session(**kwargs)


async def close_driver() -> None:
    """Close the shared driver (called on shutdown)."""
    global _driver
    if _driver is not None:
        await _driver.close()
        _driver = None


def escape_label(label: str) -> str:
    """Escape a workspace label for use inside Cypher backticks.

    Mirrors LightRAG's escaping: internal backticks are doubled.
    """
    return label.replace("`", "``")


async def verify_database(driver: AsyncDriver, database: str) -> None:
    """Fail fast when the configured database does not exist.

    Lesson from the biz_id injection incident: a missing database must raise
    loudly instead of silently reading/writing the server default.
    """
    async with get_session(database="system") as session:
        result = await session.run("SHOW DATABASES YIELD name")
        names = {record["name"] async for record in result}
    if database not in names:
        raise RuntimeError(
            f"Neo4j database '{database}' does not exist. "
            f"Available: {sorted(names)}"
        )
    logger.info("Neo4j database '%s' verified", database)
