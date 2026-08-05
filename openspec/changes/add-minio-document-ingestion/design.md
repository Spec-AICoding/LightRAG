# Design: S3-Compatible Object Storage Ingestion

## Architecture

```
┌─────────────┐     POST /documents/from-s3
│   Client    │ ──────────────────────────────────→ ┌────────────────────┐
│  (Curl/App) │                                      │   s3_routes.py      │
└─────────────┘                                      │                     │
                                                     │  ① 接收请求          │
                                                     │  ② 创建 S3 客户端    │
                                                     │  ③ 列出对象/按前缀  │
                                                     │  ④ 后台任务下载+入队│
                                                     └──────┬──────────────┘
                                                            │
                                                     ┌──────▼──────────────┐
                                                     │  INPUT_DIR  (本地)   │
                                                     │  (workspace 子目录)  │
                                                     └──────┬──────────────┘
                                                            │
                                                     ┌──────▼──────────────┐
                                                     │  document_routes.py │
                                                     │  pipeline_index_*   │
                                                     │  (零改动, 直接复用)  │
                                                     └─────────────────────┘
```

## Provider Compatibility

The `minio` Python SDK implements the **S3 API protocol**, which is supported by all major object storage providers. One SDK, one API, all providers:

| Provider | Endpoint 示例 | 用户只需改 endpoint |
|---|---|---|
| **MinIO** | `localhost:9000` | — |
| **AWS S3** | `s3.amazonaws.com` | ✅ |
| **阿里云 OSS** | `oss-cn-hangzhou.aliyuncs.com` | ✅ |
| **华为云 OBS** | `obs.cn-north-4.myhuaweicloud.com` | ✅ |
| **腾讯云 COS** | `cos.ap-guangzhou.myqcloud.com` | ✅ |
| **Ceph/Rook** | `ceph.example.com` | ✅ |
| **DigitalOcean Spaces** | `sgp1.digitaloceanspaces.com` | ✅ |

## New File: `lightrag/api/routers/s3_routes.py`

### Route Factory

```python
def create_s3_routes(
    rag: LightRAG,
    api_key: Optional[str] = None,
) -> APIRouter:
```

Follows the same factory pattern as `create_document_routes`, `create_query_routes`.

### Request Models

```python
class S3ObjectRequest(BaseModel):
    bucket: str
    key: str

class S3IngestRequest(BaseModel):
    # Connection (all optional; fall back to env vars)
    endpoint: str | None = None
    access_key: str | None = None
    secret_key: str | None = None
    region: str | None = None
    secure: bool | None = None

    # Object selection — either objects list OR prefix
    objects: list[S3ObjectRequest] | None = None
    prefix: str | None = None
    bucket: str | None = None  # required when using prefix

    # Passthrough to pipeline
    process_options: str | None = None
```

### Endpoint

```
POST /documents/from-s3 → InsertResponse
```

- Auth: same `combined_auth` dependency as other routes
- Validation: exactly one of `objects` or `prefix` must be set; if `prefix`, `bucket` is required
- Credentials priority: request body → `.env` → error
- Schedules a background task (same pattern as `/upload`):
  1. Connect to S3 via `minio` SDK
  2. If `prefix`: list all objects under `prefix`
  3. Download each object to `INPUT_DIR` (preserving basename)
  4. Call `pipeline_index_files(rag, paths, track_id)` — zero-change reuse
  5. Return `InsertResponse` with `track_id` immediately

### File Naming Strategy

Use the object basename (e.g., `prefix/to/report.pdf` → `report.pdf`). If multiple objects share the same basename, later ones get a numeric suffix appended before writing. Existing `pipeline_enqueue_file` handles cross-session duplicate detection via doc_status.

### Error Handling

- S3 connection failure → HTTP 502 with diagnostic message
- Per-object download failure → logged, other objects continue (partial success)
- Download + pipeline all in background task: errors go to server logs via existing logger

## Modified Files

### `lightrag/api/lightrag_server.py` (+2 lines)

```python
from lightrag.api.routers.s3_routes import create_s3_routes
# ... alongside existing create_* imports
app.include_router(create_s3_routes(rag, api_key))
```

### `pyproject.toml` (+1 optional dependency)

```toml
[project.optional-dependencies]
api = [
    ...
    "minio>=7.2",
]
```

### `.env.example` (optional, documentation only)

```ini
### S3-Compatible Storage Configuration (for /documents/from-s3 endpoint)
# S3_ENDPOINT_URL=http://localhost:9004
# S3_FILE_STORE_BUCKET_NAME=onyx-file-store-bucket
# S3_AWS_ACCESS_KEY_ID=minioadmin
# S3_AWS_SECRET_ACCESS_KEY=minioadmin
```

## Dependency

`minio>=7.2` — lightweight S3-compatible SDK (~200KB), natively async-compatible. Added as optional extra under `api` group. Despite the package name, it works with ANY S3-compatible service (AWS S3, Alibaba OSS, Huawei OBS, Tencent COS, Ceph, MinIO, etc.).

## Sequence Diagram (per request)

```
Client                  s3_routes.py             S3 Storage          INPUT_DIR       pipeline_index_files
  │                          │                     │                   │                    │
  │  POST /from-s3           │                     │                   │                    │
  │─────────────────────────→│                     │                   │                    │
  │                          │── validate ─────────│                   │                    │
  │                          │── list_objects() ───│→ (if prefix)      │                    │
  │                          │←── object list ─────│                   │                    │
  │                          │                     │                   │                    │
  │                          │  schedule bg task   │                   │                    │
  │  200 {track_id}          │                     │                   │                    │
  │←─────────────────────────│                     │                   │                    │
  │                          │                     │                   │                    │
  │                          │  [background]        │                   │                    │
  │                          │── fget_object() ────│→ for each obj     │                    │
  │                          │←── file content ────│                   │                    │
  │                          │── write ───────────────────────────────→│                    │
  │                          │                     │                   │                    │
  │                          │── pipeline_index_files() ───────────────────────────────────→│
  │                          │                     │                   │  enqueue + process  │
```

## Upstream Compatibility

| File | Owned by | Merge strategy |
|---|---|---|
| `s3_routes.py` | **Us** (new) | Never conflicts |
| `lightrag_server.py` | Upstream + 2 lines | Trivial auto-merge |
| `pyproject.toml` | Upstream + 1 dep | Trivial auto-merge |
| `.env.example` | Upstream + comments | Trivial auto-merge (or drop) |

No existing logic is modified. All pipeline functions are called as public stable APIs.
