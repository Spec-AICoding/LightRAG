"""
This module contains S3-compatible object storage routes for the LightRAG API.
"""

import os
import asyncio
import json
import traceback
from pathlib import Path
from typing import Optional, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, ConfigDict, model_validator

from lightrag import LightRAG
from lightrag.utils import logger, generate_track_id, validate_workspace
from lightrag.api.utils_api import internal_server_error


class S3ObjectRequest(BaseModel):
    """Request model for a single S3 object."""

    bucket: str = Field(description="S3 bucket name")
    key: str = Field(description="Object key in the bucket")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "bucket": "my-docs",
                "key": "reports/2024/summary.pdf",
            }
        }
    )


class S3IngestRequest(BaseModel):
    """Request model for S3 object ingestion.

    Connection details can be provided in the request body
    or configured via environment variables (S3_ENDPOINT,
    S3_ACCESS_KEY, S3_SECRET_KEY, S3_SECURE).
    """

    endpoint: Optional[str] = Field(
        default=None,
        description="S3 endpoint (e.g., s3.amazonaws.com, oss-cn-hangzhou.aliyuncs.com)",
    )
    access_key: Optional[str] = Field(default=None, description="S3 access key")
    secret_key: Optional[str] = Field(default=None, description="S3 secret key")
    region: Optional[str] = Field(default=None, description="S3 region")
    secure: Optional[bool] = Field(default=None, description="Use HTTPS (default: false)")

    objects: Optional[list[S3ObjectRequest]] = Field(
        default=None, description="Specific objects to download from S3"
    )
    prefix: Optional[str] = Field(
        default=None, description="S3 object key prefix to scan (requires 'bucket')"
    )
    bucket: Optional[str] = Field(
        default=None,
        description="S3 bucket name (required when using 'prefix')",
    )

    process_options: Optional[str] = Field(
        default=None, description="Processing options passthrough (i/t/e/!/F/R/V/P)"
    )
    file_type: Optional[str] = Field(
        default=None,
        description=(
            "Type hint for extensionless files, stored in doc_status metadata. "
            "E.g. 'txt', 'json', 'csv'. When set, the parser routing infers the "
            "parse engine from this value instead of the missing filename suffix."
        ),
    )

    @model_validator(mode="after")
    def validate_object_selection(self):
        has_objects = self.objects is not None and len(self.objects) > 0
        has_prefix = self.prefix is not None

        if not has_objects and not has_prefix:
            raise ValueError("Provide either 'objects' or 'prefix'")
        if has_objects and has_prefix:
            raise ValueError("Provide either 'objects' or 'prefix', not both")
        if has_prefix and not self.bucket:
            raise ValueError("'bucket' is required when using 'prefix'")
        if has_objects:
            for obj in self.objects:
                if not obj.bucket or not obj.key:
                    raise ValueError("Each object requires both 'bucket' and 'key'")
        return self

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "prefix": "",
                "bucket": "lightrag-docs",
            }
        }
    )


def _resolve_input_dir(workspace: str = "") -> Path:
    """Resolve the input directory for file downloads.

    Replicates the path logic from ``DocumentManager`` to place
    downloaded files into the correct workspace-scoped directory.
    """
    base = os.environ.get("INPUT_DIR", "./inputs")
    input_dir = Path(base)
    if workspace:
        validate_workspace(workspace)
        input_dir = input_dir / workspace
    input_dir.mkdir(parents=True, exist_ok=True)
    return input_dir


def _resolve_s3_config(request: S3IngestRequest) -> tuple:
    """Resolve S3 connection parameters from request body, falling back to env vars.

    Returns:
        tuple: (endpoint, access_key, secret_key, secure)
    """
    endpoint = request.endpoint or os.environ.get("S3_ENDPOINT_URL")
    access_key = request.access_key or os.environ.get("S3_AWS_ACCESS_KEY_ID")
    secret_key = request.secret_key or os.environ.get("S3_AWS_SECRET_ACCESS_KEY")

    secure = request.secure
    if secure is None:
        secure_str = os.environ.get("S3_SECURE", "false").lower()
        secure = secure_str in ("true", "1", "yes")

    if not endpoint or not access_key or not secret_key:
        raise HTTPException(
            status_code=400,
            detail=(
                "S3 connection details required. Provide endpoint, access_key, and "
                "secret_key in the request body, or set S3_ENDPOINT_URL, S3_AWS_ACCESS_KEY_ID, "
                "S3_AWS_SECRET_ACCESS_KEY in your .env file."
            ),
        )

    return endpoint, access_key, secret_key, secure


def create_s3_routes(
    rag: LightRAG,
    api_key: Optional[str] = None,
) -> APIRouter:
    """Create a router with S3-compatible object storage ingestion endpoints.

    Args:
        rag: The LightRAG instance.
        api_key: Optional API key for authentication.

    Returns:
        APIRouter: Configured router for S3 routes.
    """
    from minio import Minio
    from lightrag.api.routers.document_routes import (
        _reserve_enqueue_slot,
        _release_enqueue_slot,
        pipeline_index_files,
        InsertResponse,
        get_managed_background_tasks,
    )
    from lightrag.kg.shared_storage import start_reserved_background_task

    router = APIRouter(prefix="/documents", tags=["documents"])
    input_dir = _resolve_input_dir(rag.workspace)

    @router.post(
        "/from-s3",
        response_model=InsertResponse,
    )
    async def ingest_from_s3(
        request: S3IngestRequest,
        managed_tasks: set = Depends(get_managed_background_tasks),
    ):
        """
        Ingest documents from any S3-compatible object storage.

        Downloads files from S3 (AWS S3, MinIO, Alibaba OSS, Huawei OBS,
        Tencent COS, etc.) into the configured ``INPUT_DIR``, then triggers
        the standard document processing pipeline.

        **Object Selection:**
        - Use ``objects`` to download specific objects by bucket and key.
        - Use ``prefix`` + ``bucket`` to scan and download all objects
          under a given prefix.

        **S3 Connection:**
        - Provide ``endpoint``, ``access_key``, ``secret_key`` in the
          request body, or configure them via ``S3_ENDPOINT``,
          ``S3_ACCESS_KEY``, ``S3_SECRET_KEY`` in ``.env``.
        - Set ``secure=true`` for HTTPS connections.

        **Returns:**
            InsertResponse with ``track_id`` for monitoring progress via
            ``GET /documents/track_status/{track_id}``.
        """
        enqueue_token = uuid4().hex
        handed_off = False

        try:
            # Resolve S3 config synchronously (fail fast on bad config)
            endpoint, access_key, secret_key, secure = _resolve_s3_config(request)

            # Log request parameters (mask credentials) in JSON format
            masked_secret = (secret_key[:4] + "****") if secret_key else None
            masked_access = (access_key[:4] + "****") if access_key else None
            mode = "objects" if request.objects else "prefix"
            objects_count = len(request.objects) if request.objects else 0
            bucket_display = request.bucket or "-"
            prefix_display = repr(request.prefix) if request.prefix is not None else "-"
            logger.info(
                "[S3 Ingestion] Request received: "
                + json.dumps(
                    {
                        "endpoint": endpoint,
                        "secure": secure,
                        "access_key": masked_access,
                        "secret_key": masked_secret,
                        "mode": mode,
                        "objects_count": objects_count,
                        "bucket": bucket_display,
                        "prefix": prefix_display,
                        "file_type": request.file_type,
                        "process_options": request.process_options,
                    },
                    ensure_ascii=False,
                )
            )
            if request.objects:
                logger.info(
                    "[S3 Ingestion] Request objects: "
                    + json.dumps(
                        [
                            {"bucket": obj.bucket, "key": obj.key}
                            for obj in request.objects
                        ],
                        ensure_ascii=False,
                    )
                )
            elif request.prefix is not None and request.bucket:
                logger.info(
                    "[S3 Ingestion] Request prefix mode: "
                    + json.dumps(
                        {"bucket": request.bucket, "prefix": request.prefix},
                        ensure_ascii=False,
                    )
                )

            # Reserve enqueue slot (same concurrency contract as /upload)
            await _reserve_enqueue_slot(rag, enqueue_token)

            # Build the list of objects to download
            s3_objects: list[tuple[str, str]] = []
            if request.objects:
                s3_objects = [(obj.bucket, obj.key) for obj in request.objects]
            elif request.prefix is not None and request.bucket:
                # List mode — we'll resolve this in the background task
                # so slow list_objects doesn't block the response.
                list_mode = True
                list_bucket = request.bucket
                list_prefix = request.prefix
            else:
                raise HTTPException(
                    status_code=400,
                    detail="Provide either 'objects' or 'prefix' with 'bucket'.",
                )

            track_id = generate_track_id("s3")

            async def _ingest_work(started):
                started.set()
                try:
                    # Create S3 client inside the bg task so the connection
                    # lifecycle is scoped to the download window.
                    # Strip scheme prefix if present — Minio SDK only accepts
                    # host:port, not http://host:port.
                    clean_endpoint = endpoint
                    for prefix in ("http://", "https://"):
                        if clean_endpoint.startswith(prefix):
                            clean_endpoint = clean_endpoint[len(prefix) :]
                            break
                    client: Minio = Minio(
                        endpoint=clean_endpoint,
                        access_key=access_key,
                        secret_key=secret_key,
                        secure=secure,
                    )

                    # Resolve object list
                    resolved_objects: list[tuple[str, str]] = []
                    if s3_objects:
                        resolved_objects = s3_objects
                    else:
                        # list_objects returns an iterator; collect eagerly
                        obj_iter = client.list_objects(
                            list_bucket, prefix=list_prefix, recursive=True
                        )
                        for obj in obj_iter:
                            resolved_objects.append((obj.bucket_name, obj.object_name))

                    if not resolved_objects:
                        logger.info(
                            "[S3 Ingestion] No objects found matching the request"
                        )
                        return

                    # Download each object
                    downloaded_paths: list[Path] = []
                    for bucket, key in resolved_objects:
                        try:
                            filename = Path(key).name or key
                            # Files without an extension use file_type from
                            # the request as their suffix, so the parser
                            # routing picks the correct engine.
                            if not Path(filename).suffix:
                                ext = (request.file_type or "txt").lstrip(".")
                                filename = f"{filename}.{ext}"
                            dest_path = input_dir / filename

                            # Handle same-basename collisions
                            if dest_path.exists():
                                stem = dest_path.stem
                                ext = dest_path.suffix
                                counter = 1
                                while dest_path.exists():
                                    dest_path = input_dir / f"{stem}_{counter}{ext}"
                                    counter += 1

                            await asyncio.to_thread(
                                client.fget_object, bucket, key, str(dest_path)
                            )
                            downloaded_paths.append(dest_path)
                            logger.debug(
                                f"[S3 Ingestion] Downloaded s3://{bucket}/{key} -> {dest_path}"
                            )
                        except Exception as download_error:
                            logger.error(
                                f"[S3 Ingestion] Failed to download s3://{bucket}/{key}: {download_error}"
                            )
                            continue

                    if downloaded_paths:
                        await pipeline_index_files(
                            rag, downloaded_paths, track_id
                        )
                        logger.info(
                            f"[S3 Ingestion] Enqueued {len(downloaded_paths)} files "
                            f"from S3 for processing"
                        )

                        # Store file_type in doc_status metadata if provided
                        if request.file_type:
                            for dl_path in downloaded_paths:
                                doc = await rag.doc_status.get_doc_by_file_path(
                                    str(dl_path.resolve())
                                )
                                if doc is None:
                                    doc = await rag.doc_status.get_doc_by_file_path(
                                        str(dl_path)
                                    )
                                if doc:
                                    doc_id = doc.get("doc_id")
                                    if doc_id:
                                        existing_meta = doc.get("metadata") or {}
                                        existing_meta["file_type"] = request.file_type
                                        await rag.doc_status.upsert(
                                            {doc_id: {"metadata": existing_meta}}
                                        )
                except HTTPException:
                    raise
                except Exception as exc:
                    logger.error(f"[S3 Ingestion] S3 connection error: {exc}")
                    logger.error(traceback.format_exc())
                    raise HTTPException(
                        status_code=502,
                        detail=f"S3 connection failed: {exc}",
                    )
                finally:
                    await _release_enqueue_slot(rag, enqueue_token)

            async def _enqueue_backstop():
                await _release_enqueue_slot(rag, enqueue_token)

            await start_reserved_background_task(
                managed_tasks,
                work=_ingest_work,
                backstop_release=_enqueue_backstop,
            )
            handed_off = True

            response = InsertResponse(
                status="success",
                message=(
                    "S3 ingestion started. Files will be downloaded and "
                    "processed in the background."
                ),
                track_id=track_id,
            )
            logger.info(
                "[S3 Ingestion] Response sent: "
                + json.dumps(
                    {
                        "status": response.status,
                        "message": response.message,
                        "track_id": response.track_id,
                    },
                    ensure_ascii=False,
                )
            )
            return response

        except HTTPException:
            raise
        except Exception as exc:
            logger.error(f"[S3 Ingestion] Error: {exc}")
            logger.error(traceback.format_exc())
            raise internal_server_error(exc)
        finally:
            if not handed_off:
                await _release_enqueue_slot(rag, enqueue_token)

    return router
