"""ACL sync endpoint — pushed to by the onyx background worker (no auth)."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..acl_sync import sync_acl
from ..config import get_config

router = APIRouter(prefix="/acl", tags=["acl"])


class AclDocument(BaseModel):
    doc_id: str = Field(..., min_length=1)
    external_user_emails: list[str] = Field(default_factory=list)
    external_user_group_ids: list[str] = Field(default_factory=list)
    is_public: bool = False


class AclSyncRequest(BaseModel):
    docs: list[AclDocument] = Field(default_factory=list)


@router.post("/sync")
async def acl_sync(payload: AclSyncRequest) -> dict:
    """Idempotent ACL sync: upsert snapshots then recompute entity ACLs.

    An empty batch returns 200 with zero counts and performs no writes.
    """
    if not get_config().acl_sync_enabled:
        raise HTTPException(status_code=403, detail="ACL sync is disabled")
    return await sync_acl([doc.model_dump() for doc in payload.docs])
