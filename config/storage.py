"""Media storage backends.

Filesystem is the default. When ``MEDIA_BACKEND=s3`` the default storage is Amazon S3
(or any S3-compatible endpoint) via django-storages. Object storage is a configuration
change, not a code path that requires forking the product.
"""

from __future__ import annotations

from django.core.exceptions import ImproperlyConfigured


def build_default_storage(*, backend: str, env_str, env_bool) -> dict:
    """Return the ``STORAGES['default']`` entry for the configured media backend."""

    backend = (backend or "filesystem").strip().lower()
    if backend in {"", "filesystem", "local"}:
        return {"BACKEND": "django.core.files.storage.FileSystemStorage"}

    if backend != "s3":
        raise ImproperlyConfigured(
            f"MEDIA_BACKEND must be filesystem or s3, got {backend!r}."
        )

    bucket = env_str("AWS_STORAGE_BUCKET_NAME", "")
    if not bucket:
        raise ImproperlyConfigured(
            "MEDIA_BACKEND=s3 requires AWS_STORAGE_BUCKET_NAME."
        )

    options: dict = {
        "bucket_name": bucket,
        "default_acl": env_str("AWS_DEFAULT_ACL", "private") or None,
        "querystring_auth": env_bool("AWS_QUERYSTRING_AUTH", True),
        "file_overwrite": env_bool("AWS_S3_FILE_OVERWRITE", False),
        "object_parameters": {"CacheControl": "max-age=86400"},
    }
    region = env_str("AWS_S3_REGION_NAME", "")
    if region:
        options["region_name"] = region
    endpoint = env_str("AWS_S3_ENDPOINT_URL", "")
    if endpoint:
        options["endpoint_url"] = endpoint
    custom_domain = env_str("AWS_S3_CUSTOM_DOMAIN", "")
    if custom_domain:
        options["custom_domain"] = custom_domain
    access_key = env_str("AWS_ACCESS_KEY_ID", "")
    secret_key = env_str("AWS_SECRET_ACCESS_KEY", "")
    if access_key:
        options["access_key"] = access_key
    if secret_key:
        options["secret_key"] = secret_key

    return {"BACKEND": "storages.backends.s3.S3Storage", "OPTIONS": options}
