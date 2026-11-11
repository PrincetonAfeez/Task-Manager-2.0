# Attachment storage

Uploads (task attachments) use Django's default file storage. The backend is selected by
environment, not by code forks.

| `MEDIA_BACKEND` | Behaviour |
|-----------------|-----------|
| `filesystem` (default) | Files under `MEDIA_ROOT` (compose volume in Docker) |
| `s3` | S3-compatible object storage via django-storages |

## S3 / MinIO / R2

```bash
MEDIA_BACKEND=s3
AWS_STORAGE_BUCKET_NAME=task-manager-attachments
AWS_S3_REGION_NAME=us-east-1
AWS_ACCESS_KEY_ID=…
AWS_SECRET_ACCESS_KEY=…
# Optional — required for MinIO and many S3-compatible hosts:
# AWS_S3_ENDPOINT_URL=https://minio.example.com
# AWS_S3_CUSTOM_DOMAIN=cdn.example.com
AWS_QUERYSTRING_AUTH=1
AWS_S3_FILE_OVERWRITE=0
AWS_DEFAULT_ACL=private
```

Private buckets with querystring auth are the intended production shape. Public buckets
are not recommended; attachment download URLs already go through the application so scan
status and tenancy can be enforced.

Tests always use the filesystem backend regardless of env.
