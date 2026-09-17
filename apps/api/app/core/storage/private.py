"""Private bucket (KYC, return photos, invoices). Nothing here is ever served by the CDN.

Uploads go straight from the client to storage with a short-lived signed PUT; the API never
proxies file bytes. Reads are 5-minute signed GETs minted only for authorised staff."""

import pathlib
import time
from dataclasses import dataclass
from typing import Protocol

import jwt


@dataclass
class SignedUpload:
    url: str
    method: str
    headers: dict
    key: str
    expires_in: int
    max_bytes: int


class PrivateStorage(Protocol):
    def presign_put(
        self, key: str, *, content_type: str, max_bytes: int, ttl: int = 600
    ) -> SignedUpload: ...

    def presign_get(self, key: str, *, ttl: int = 300) -> str: ...

    async def head(self, key: str) -> int | None: ...

    async def put(self, key: str, data: bytes, content_type: str) -> None: ...


class LocalPrivateStorage:
    """Dev/test: signed tokens handled by /internal/private/{token}."""

    def __init__(self, root: str, secret: str, base_url: str = ""):
        self.root = pathlib.Path(root)
        self.secret = secret
        self.base_url = base_url

    def _token(self, claims: dict, ttl: int) -> str:
        now = int(time.time())
        return jwt.encode({**claims, "iat": now, "exp": now + ttl}, self.secret, algorithm="HS256")

    def decode(self, token: str) -> dict:
        return jwt.decode(token, self.secret, algorithms=["HS256"])

    def path(self, key: str) -> pathlib.Path:
        p = (self.root / key).resolve()
        if not str(p).startswith(str(self.root.resolve())):
            raise ValueError("invalid key")
        return p

    def presign_put(
        self, key: str, *, content_type: str, max_bytes: int, ttl: int = 600
    ) -> SignedUpload:
        tok = self._token(
            {"typ": "priv_put", "key": key, "ct": content_type, "max": max_bytes}, ttl
        )
        return SignedUpload(
            url=f"{self.base_url}/internal/private/{tok}",
            method="PUT",
            headers={"content-type": content_type},
            key=key,
            expires_in=ttl,
            max_bytes=max_bytes,
        )

    def presign_get(self, key: str, *, ttl: int = 300) -> str:
        return (
            f"{self.base_url}/internal/private/{self._token({'typ': 'priv_get', 'key': key}, ttl)}"
        )

    async def head(self, key: str) -> int | None:
        p = self.path(key)
        return p.stat().st_size if p.exists() else None

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        """Server-generated documents (tax invoices, credit notes) are written directly; buyer and
        vendor uploads still go through a signed PUT so bytes never pass through the API."""
        p = self.path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


class S3PrivateStorage:
    def __init__(self, client, bucket: str):
        self.client = client
        self.bucket = bucket

    def presign_put(
        self, key: str, *, content_type: str, max_bytes: int, ttl: int = 600
    ) -> SignedUpload:
        url = self.client.generate_presigned_url(
            "put_object",
            Params={"Bucket": self.bucket, "Key": key, "ContentType": content_type},
            ExpiresIn=ttl,
        )
        return SignedUpload(
            url=url,
            method="PUT",
            headers={"content-type": content_type},
            key=key,
            expires_in=ttl,
            max_bytes=max_bytes,
        )

    def presign_get(self, key: str, *, ttl: int = 300) -> str:
        return self.client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=ttl
        )

    async def head(self, key: str) -> int | None:
        import asyncio

        try:
            meta = await asyncio.to_thread(self.client.head_object, Bucket=self.bucket, Key=key)
        except Exception:  # noqa: BLE001
            return None
        return int(meta["ContentLength"])

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        import asyncio

        await asyncio.to_thread(
            self.client.put_object,
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
        )
