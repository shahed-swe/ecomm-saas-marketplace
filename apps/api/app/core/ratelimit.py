from app.core.errors import AppError


class RateLimited(AppError):
    status = 429
    code = "rate_limited"


async def hit(redis, key: str, limit: int, window_seconds: int) -> None:
    """Fixed window counter. Raise 429 when the limit is exceeded."""
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, window_seconds)
    if count > limit:
        raise RateLimited("Too many attempts, try again later")
