"""Magicbox backend: read-only Neo4j graph retrieval for LightRAG.

Standalone FastAPI service with no authentication (internal network use).
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import get_config
from .neo4j import close_driver, escape_label, get_driver, get_session, verify_database
from .routers import acl, entities, subgraph

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("magicbox")


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = get_config()
    driver = get_driver()
    # Fail fast on a missing database instead of silently hitting the
    # server default (see the biz_id injection incident).
    await verify_database(driver, cfg.neo4j_database)
    logger.info(
        "Magicbox backend ready: database=%s workspace=%s",
        cfg.neo4j_database,
        cfg.workspace,
    )
    yield
    await close_driver()


def create_app() -> FastAPI:
    cfg = get_config()
    app = FastAPI(title="Magicbox Backend", version="0.1.0", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    app.include_router(entities.router)
    app.include_router(subgraph.router)
    app.include_router(acl.router)

    @app.get("/health", tags=["health"])
    async def health() -> dict:
        """Liveness plus Neo4j connectivity and entity count."""
        label = f"`{escape_label(cfg.workspace)}`"
        async with get_session() as session:
            result = await session.run(
                f"MATCH (n:{label}) RETURN count(n) AS count"
            )
            record = await result.single()
            await result.consume()
        return {
            "status": "ok",
            "database": cfg.neo4j_database,
            "label": cfg.workspace,
            "entity_count": record["count"],
        }

    return app


app = create_app()
