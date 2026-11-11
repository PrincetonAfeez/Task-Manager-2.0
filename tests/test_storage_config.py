"""MEDIA_BACKEND selection."""

from __future__ import annotations

import pytest
from django.core.exceptions import ImproperlyConfigured

from config.storage import build_default_storage


def test_filesystem_backend():
    cfg = build_default_storage(
        backend="filesystem",
        env_str=lambda *a, **k: "",
        env_bool=lambda *a, **k: False,
    )
    assert cfg["BACKEND"].endswith("FileSystemStorage")


def test_s3_requires_bucket():
    with pytest.raises(ImproperlyConfigured):
        build_default_storage(
            backend="s3",
            env_str=lambda key, default="": default,
            env_bool=lambda *a, **k: True,
        )


def test_s3_builds_options():
    values = {
        "AWS_STORAGE_BUCKET_NAME": "tm-attachments",
        "AWS_S3_REGION_NAME": "eu-west-1",
        "AWS_ACCESS_KEY_ID": "AKIA",
        "AWS_SECRET_ACCESS_KEY": "secret",
        "AWS_S3_ENDPOINT_URL": "https://minio.example",
        "AWS_DEFAULT_ACL": "private",
    }

    def env_str(key, default=""):
        return values.get(key, default)

    cfg = build_default_storage(backend="s3", env_str=env_str, env_bool=lambda *a, **k: True)
    assert cfg["BACKEND"] == "storages.backends.s3.S3Storage"
    assert cfg["OPTIONS"]["bucket_name"] == "tm-attachments"
    assert cfg["OPTIONS"]["endpoint_url"] == "https://minio.example"
