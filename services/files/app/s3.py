"""S3 client wrapper (R41: user-provided host, config from env)."""
import aioboto3

from .config import env_fallback, settings


def s3_kwargs() -> dict:
    kw = {
        "aws_access_key_id": env_fallback(settings.s3_access_key_env),
        "aws_secret_access_key": env_fallback(settings.s3_secret_key_env),
    }
    if settings.s3_endpoint:
        kw["endpoint_url"] = settings.s3_endpoint
    if settings.s3_region:
        kw["region_name"] = settings.s3_region
    return kw


def session() -> aioboto3.Session:
    return aioboto3.Session()


async def ensure_bucket() -> None:
    async with session().client("s3", **s3_kwargs()) as s3:
        try:
            await s3.head_bucket(Bucket=settings.s3_bucket)
        except Exception:
            await s3.create_bucket(Bucket=settings.s3_bucket)


async def put(s3_key: str, data: bytes, content_type: str = "") -> None:
    async with session().client("s3", **s3_kwargs()) as s3:
        await s3.put_object(Bucket=settings.s3_bucket, Key=s3_key,
                            Body=data, ContentType=content_type or "application/octet-stream")


async def get(s3_key: str) -> bytes:
    async with session().client("s3", **s3_kwargs()) as s3:
        r = await s3.get_object(Bucket=settings.s3_bucket, Key=s3_key)
        async with r["Body"] as body:
            return await body.read()


async def delete(s3_key: str) -> None:
    async with session().client("s3", **s3_kwargs()) as s3:
        await s3.delete_object(Bucket=settings.s3_bucket, Key=s3_key)


async def presign_get(s3_key: str, expires_s: int = 900) -> str:
    async with session().client("s3", **s3_kwargs()) as s3:
        return await s3.generate_presigned_url(
            "get_object", Params={"Bucket": settings.s3_bucket, "Key": s3_key},
            ExpiresIn=expires_s)
