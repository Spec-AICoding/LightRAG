"""Read-only entity search endpoints."""

from fastapi import APIRouter, HTTPException, Query

from ..config import get_config
from ..neo4j import escape_label, get_session

router = APIRouter(prefix="/entities", tags=["entities"])

# Whitelist: property name -> match kind. List properties use exact `IN`
# membership; string properties use `CONTAINS`; bool properties use
# string-to-boolean equality. Anything else is rejected with 400 so
# arbitrary property access stays impossible. Property names are
# interpolated into backticked identifiers only after this check, so no
# injection is possible.
PROPERTY_WHITELIST: dict[str, str] = {
    "biz_id": "list",
    "biz_name": "list",
    "semantic_identifier": "list",
    "connector_source": "string",
    "connector_name": "string",
    "external_user_emails": "list",
    "external_user_group_ids": "list",
    "is_public": "bool",
    "entity_id": "string",
    "entity_type": "string",
    "description": "string",
    "file_path": "string",
    "source_id": "string",
}


def _label() -> str:
    return f"`{escape_label(get_config().workspace)}`"


@router.get("/search")
async def search_entities(
    property: str = Query(..., description="Entity property to search"),
    value: str = Query(..., min_length=1, max_length=512),
    limit: int = Query(20, ge=1, le=100),
) -> dict:
    """Search entities by a whitelisted property."""
    match_kind = PROPERTY_WHITELIST.get(property)
    if match_kind is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unsupported property '{property}'. "
                f"Allowed: {sorted(PROPERTY_WHITELIST)}"
            ),
        )

    if match_kind == "list":
        where_clause = f"$value IN n.`{property}`"
    elif match_kind == "bool":
        where_clause = f"n.`{property}` = ($value IN ['true', '1', 'yes', 'TRUE'])"
    else:
        where_clause = f"n.`{property}` CONTAINS $value"

    cfg = get_config()
    async with get_session() as session:
        count_result = await session.run(
            f"""
            MATCH (n:{_label()})
            WHERE {where_clause}
            RETURN count(n) AS total
            """,
            value=value,
        )
        total = (await count_result.single())["total"]
        await count_result.consume()

        result = await session.run(
            f"""
            MATCH (n:{_label()})
            WHERE {where_clause}
            RETURN properties(n) AS node
            ORDER BY n.entity_id
            LIMIT $limit
            """,
            value=value,
            limit=limit,
        )
        entities = [record["node"] async for record in result]
        await result.consume()

    return {"total": total, "entities": entities}


@router.get("/{entity_id:path}")
async def get_entity(entity_id: str) -> dict:
    """Return a single entity's full property map."""
    cfg = get_config()
    async with get_session() as session:
        result = await session.run(
            f"""
            MATCH (n:{_label()} {{entity_id: $id}})
            RETURN properties(n) AS node
            """,
            id=entity_id,
        )
        record = await result.single()
        await result.consume()

    if record is None:
        raise HTTPException(
            status_code=404, detail=f"Entity '{entity_id}' not found"
        )
    return {"entity": record["node"]}
