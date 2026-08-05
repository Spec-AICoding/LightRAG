# Tasks: S3-Compatible Object Storage Ingestion

## 1. Add dependency

**File**: `pyproject.toml`

- [x] Add `minio>=7.2` to `[project.optional-dependencies]` under the `api` extra

```toml
[project.optional-dependencies]
api = [
    ...
    "minio>=7.2",
]
```

- [x] `minio>=7.2` installed successfully

---

## 2. Create `lightrag/api/routers/s3_routes.py`

**File**: `lightrag/api/routers/s3_routes.py` (new, ~150 lines)

### 2a. Pydantic models

- [x] `S3ObjectRequest` — `bucket: str`, `key: str`
- [x] `S3IngestRequest` — connection fields (all optional, fallback to env), object selection (`objects: list[S3ObjectRequest]` | `prefix: str` + `bucket: str`), `process_options: str | None`

### 2b. Route factory

```python
def create_s3_routes(rag: LightRAG, api_key: Optional[str] = None) -> APIRouter:
```

Follow the same pattern as `create_document_routes`:
- Fresh `APIRouter(prefix="/documents", tags=["documents"])` per call
- `combined_auth = get_combined_auth_dependency(api_key)`

### 2c. `POST /documents/from-s3` endpoint

- **Validation**: exactly one of `objects` or `prefix+bucket` must be set
- **Reserve enqueue slot**: call `_reserve_enqueue_slot(rag, token)` (imported from document_routes)
- **Background task** (same pattern as `/upload`):
  1. Build MinIO client (`minio.Minio`) from request body fields → `.env` fallback via `os.environ.get()`
     - `Minio(endpoint, access_key=..., secret_key=..., secure=...)`
  2. If `prefix` mode: `client.list_objects(bucket, prefix=prefix, recursive=True)`
  3. Download each object to `INPUT_DIR / basename(object.key)`
     - Handle same-basename collisions with numeric suffix
     - Use `asyncio.to_thread(client.fget_object(...))` for blocking I/O
  4. Collect all downloaded paths
  5. Call `pipeline_index_files(rag, paths, track_id)` (imported from document_routes)
  6. Release enqueue slot in `finally`
- **Return**: `InsertResponse(status="success", track_id=..., message=...)` immediately
- **Error handling**:
  - S3 connection failure → `HTTPException(502)`
  - Per-object download failure → log + continue with next
  - Catch-all → `internal_server_error(e)`

### 2d. Helper: `_build_s3_client(config: S3IngestRequest) -> Minio`

Resolve endpoint/access_key/secret_key/secure from request body, falling back to env vars `S3_ENDPOINT`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_SECURE`.

---

## 3. Register route in `lightrag_server.py`

**File**: `lightrag/api/lightrag_server.py`

- [x] Add import: `from lightrag.api.routers.s3_routes import create_s3_routes`
- [x] Add alongside existing `include_router` lines:
  ```python
  app.include_router(create_s3_routes(rag, api_key))
  ```

- [x] Module syntax OK (`ast.parse` passes)
- [x] Server file syntax OK

---

## 4. Update `.env.example` (documentation)

**File**: `.env.example`

- [x] Added commented-out S3 configuration block after existing storage sections

```ini
### S3-Compatible Storage Configuration (for /documents/from-s3 endpoint)
# S3_ENDPOINT=localhost:9000
# S3_ACCESS_KEY=minioadmin
# S3_SECRET_KEY=minioadmin
# S3_BUCKET=lightrag-docs
# S3_REGION=us-east-1
# S3_SECURE=false
```

---

## 5. Test

**Prerequisites**: Start a local MinIO instance or configure with an accessible S3-compatible endpoint

- [x] Module syntax verified (`ast.parse` passes)
- [x] Model validation tested (objects, prefix, error cases)

**Manual test (MinIO)**:
1. Start server: `lightrag-server`
2. Upload to MinIO: `mc cp test.pdf myminio/lightrag-docs/`
3. Trigger ingestion:
   ```bash
   curl -X POST http://localhost:9621/documents/from-s3 \
     -H "Content-Type: application/json" \
     -d '{"prefix": "", "bucket": "lightrag-docs"}'
   ```
4. Check status: `GET /documents/track_status/{track_id}`
5. Query: `POST /query "What's in the document?"`

**Manual test (specific objects)**:
```bash
curl -X POST http://localhost:9621/documents/from-s3 \
  -H "Content-Type: application/json" \
  -d '{"objects": [{"bucket": "my-docs", "key": "report.pdf"}], "endpoint": "oss-cn-hangzhou.aliyuncs.com", "access_key": "...", "secret_key": "..."}'
```

**Edge cases to verify**:
- Empty prefix (no matching objects) → graceful empty response
- Object with same basename as existing `INPUT_DIR` file → handled by existing dedup
- S3 connection refused → HTTP 502
- Wrong credentials → HTTP 502 with auth error message
- Mixed file types (.pdf, .docx, .txt) → each routed to correct parser
