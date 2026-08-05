# Add S3-Compatible Object Storage Ingestion

## Summary

Add a new API endpoint to ingest documents from any S3-compatible object storage (MinIO, AWS S3, Alibaba OSS, Huawei OBS, Tencent COS, etc.), enabling knowledge parsing without manual file uploads.

## Motivation

Currently, LightRAG only supports document ingestion via:
- `POST /documents/upload` — manual HTTP file upload
- `POST /documents/text` / `POST /documents/texts` — raw text insertion
- `POST /documents/scan` — scan local `INPUT_DIR`

Users who already store documents in object storage must download them first, then re-upload — an unnecessary round-trip. A direct S3-compatible integration eliminates this friction across all major cloud providers.

## Goals

- New `POST /documents/from-s3` endpoint that accepts S3 connection details + object keys, downloads files, and triggers the existing pipeline
- Compatible with all S3-compatible providers: MinIO, AWS S3, Alibaba OSS, Huawei OBS, Tencent COS, Ceph, etc.
- Optional `.env`-based S3 configuration for reusable credentials
- Full reuse of existing pipeline (parser engine detection, chunking, dedup, status tracking)
- Zero or minimal changes to upstream-maintained files

## Non-goals

- Not a bucket notification listener / S3 event watcher (can be added later)
- Not replacing `INPUT_DIR` — S3 is an additional source
- Not modifying `DocumentManager`, `pipeline_enqueue_file`, or any core pipeline logic
