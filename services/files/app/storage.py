"""Byte storage abstraction (issue #51 A): two backends behind identical
functions — put/get/delete/presign_get/ensure_bucket.

- S3 backend (prod, R41 user-provided host, config from TG_FILES_S3_*):
  the ONLY place the boto layer is imported; no service code touches it.
- In-memory dict backend selected by TG_DEV=1 (mirrors the dev-service-token
  pattern): bytes live in process memory and vanish on restart — dev/test
  only, never a production path.

Backend choice is resolved per call so tests can flip the env around a
single process without a restart.
"""
import os


def _dev_mode() -> bool:
    return os.environ.get("TG_DEV", "") == "1"


# ---------------- in-memory backend (TG_DEV=1) ----------------

_MEM: dict[str, tuple[bytes, str]] = {}     # key -> (data, content_type)


class _Memory:
    @staticmethod
    async def ensure_bucket() -> None:
        return None

    @staticmethod
    async def put(key: str, data: bytes, content_type: str = "") -> None:
        _MEM[key] = (data, content_type or "application/octet-stream")

    @staticmethod
    async def get(key: str) -> bytes:
        try:
            return _MEM[key][0]
        except KeyError:
            raise FileNotFoundError(f"no such object: {key}")

    @staticmethod
    async def delete(key: str) -> None:
        _MEM.pop(key, None)

    @staticmethod
    async def presign_get(key: str, expires_s: int = 900) -> str:
        # Not a real URL by design: there is no object server in dev mode.
        return f"tg-memory://{key}?expires_s={expires_s}"


# ---------------- S3 backend (prod) ----------------

class _S3:
    """Thin aioboto3 adapter (R41: user-provided host, config from env)."""

    @staticmethod
    def _kwargs() -> dict:
        from .config import env_fallback, settings
        kw = {
            "aws_access_key_id": settings.s3_access_key or env_fallback("TG_S3_ACCESS_KEY"),
            "aws_secret_access_key": settings.s3_secret_key or env_fallback("TG_S3_SECRET_KEY"),
        }
        if settings.s3_endpoint:
            ep = settings.s3_endpoint
            if not ep.startswith(("http://", "https://")):
                ep = f"https://{ep}"
            kw["endpoint_url"] = ep
        if settings.s3_region:
            kw["region_name"] = settings.s3_region
        return kw

    @staticmethod
    def _session():
        import aioboto3
        return aioboto3.Session()

    @staticmethod
    async def ensure_bucket() -> None:
        from .config import settings
        async with _S3._session().client("s3", **_S3._kwargs()) as s3:
            try:
                await s3.head_bucket(Bucket=settings.s3_bucket)
            except Exception:
                await s3.create_bucket(Bucket=settings.s3_bucket)

    @staticmethod
    async def put(key: str, data: bytes, content_type: str = "") -> None:
        from .config import settings
        async with _S3._session().client("s3", **_S3._kwargs()) as s3:
            await s3.put_object(Bucket=settings.s3_bucket, Key=key, Body=data,
                                ContentType=content_type or "application/octet-stream")

    @staticmethod
    async def get(key: str) -> bytes:
        from .config import settings
        async with _S3._session().client("s3", **_S3._kwargs()) as s3:
            r = await s3.get_object(Bucket=settings.s3_bucket, Key=key)
            async with r["Body"] as body:
                return await body.read()

    @staticmethod
    async def delete(key: str) -> None:
        from .config import settings
        async with _S3._session().client("s3", **_S3._kwargs()) as s3:
            await s3.delete_object(Bucket=settings.s3_bucket, Key=key)

    @staticmethod
    async def presign_get(key: str, expires_s: int = 900) -> str:
        from .config import settings
        async with _S3._session().client("s3", **_S3._kwargs()) as s3:
            return await s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": settings.s3_bucket, "Key": key},
                ExpiresIn=expires_s)


def _backend():
    return _Memory if _dev_mode() else _S3


# ---------------- the identical functions every caller uses ----------------

async def ensure_bucket() -> None:
    await _backend().ensure_bucket()


async def put(key: str, data: bytes, content_type: str = "") -> None:
    await _backend().put(key, data, content_type)


async def get(key: str) -> bytes:
    return await _backend().get(key)


async def delete(key: str) -> None:
    await _backend().delete(key)


async def presign_get(key: str, expires_s: int = 900) -> str:
    return await _backend().presign_get(key, expires_s)
