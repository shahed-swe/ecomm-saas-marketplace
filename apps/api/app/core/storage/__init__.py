"""Object storage behind one interface (ADR 0013: MinIO/S3 in every environment but tests/dev)."""

import asyncio
import pathlib
from typing import Protocol

from starlette.concurrency import run_in_threadpool


class ObjectStorage(Protocol):
    async def put(
        self, key: str, data: bytes, *, content_type: str, cache_control: str
    ) -> None: ...

    def public_path(self, key: str) -> str: ...


class LocalStorage:
    """Dev/test implementation. Keys are already tenant-prefixed (cache_keys.object_key)."""

    def __init__(self, root: str):
        self.root = pathlib.Path(root)

    async def put(self, key: str, data: bytes, *, content_type: str, cache_control: str) -> None:
        path = (self.root / key).resolve()
        if not str(path).startswith(str(self.root.resolve())):
            raise ValueError("invalid key")

        def _write():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

        await run_in_threadpool(_write)

    def public_path(self, key: str) -> str:
        return f"/media/{key}"


class S3Storage:
    def __init__(
        self,
        *,
        endpoint: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        region: str = "us-east-1",
    ):
        import boto3

        self.bucket = bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
        )

    async def put(self, key: str, data: bytes, *, content_type: str, cache_control: str) -> None:
        await asyncio.to_thread(
            self.client.put_object,
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
            CacheControl=cache_control,
        )

    def public_path(self, key: str) -> str:
        return f"/media/{key}"


def build_storage(settings) -> ObjectStorage:
    if settings.s3_endpoint:
        return S3Storage(
            endpoint=settings.s3_endpoint,
            bucket=settings.s3_public_bucket,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
        )
    return LocalStorage(settings.local_media_root)
