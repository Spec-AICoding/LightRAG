"""Configuration for the magicbox backend.

The service shares Neo4j connection settings with the official LightRAG
server: it loads the repo-root ``.env`` first, then an optional local
``magicbox/backend/.env`` on top (local wins).
"""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_DIR.parent.parent

_CONFIG: "Config | None" = None


@dataclass(frozen=True)
class Config:
    neo4j_uri: str
    neo4j_username: str
    neo4j_password: str
    neo4j_database: str
    workspace: str
    port: int
    cors_origins: list[str]
    acl_sync_enabled: bool


def _load_env() -> None:
    # Root first (shared with LightRAG), local second with override=True so
    # magicbox/backend/.env wins over the root file.
    load_dotenv(REPO_ROOT / ".env")
    load_dotenv(BACKEND_DIR / ".env", override=True)


def get_config() -> Config:
    global _CONFIG
    if _CONFIG is None:
        _load_env()
        _CONFIG = Config(
            neo4j_uri=os.getenv("NEO4J_URI", "bolt://localhost:7687"),
            neo4j_username=os.getenv("NEO4J_USERNAME", "neo4j"),
            neo4j_password=os.getenv("NEO4J_PASSWORD", ""),
            neo4j_database=os.getenv("NEO4J_DATABASE", "neo4j"),
            workspace=os.getenv("NEO4J_WORKSPACE", "base"),
            port=int(os.getenv("MAGICBOX_PORT", "9622")),
            cors_origins=[
                origin.strip()
                for origin in os.getenv(
                    "MAGICBOX_CORS_ORIGINS", "http://localhost:5174"
                ).split(",")
                if origin.strip()
            ],
            # Endpoint stays mounted either way; the switch gates writes.
            acl_sync_enabled=os.getenv("ACL_SYNC_ENABLED", "true").lower()
            == "true",
        )
    return _CONFIG
