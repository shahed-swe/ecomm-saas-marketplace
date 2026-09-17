from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="APP_", extra="ignore")

    env: Literal["local", "test", "staging", "production"] = "local"
    service_name: str = "ecomm-api"
    # App role: NO BYPASSRLS. Platform role is used only by app/platform jobs.
    database_url: str = "postgresql+asyncpg://app:app@localhost:5432/ecomm"
    platform_database_url: str = "postgresql+asyncpg://platform:platform@localhost:5432/ecomm"
    redis_url: str = "redis://localhost:6379/0"
    platform_root_domain: str = "localhost"
    # Edge (Caddy) public IPs a custom domain may point A/AAAA records at.
    edge_ips: list[str] = []
    jwt_secret: str = Field(default="change-me-in-env-change-me-in-env", min_length=32)
    jwt_access_ttl_seconds: int = 900
    refresh_ttl_buyer_days: int = 30
    refresh_ttl_staff_hours: int = 12
    otp_secret: str = Field(default="change-me-otp-secret-change-me-otp", min_length=32)
    otp_ttl_seconds: int = 300
    otp_max_attempts: int = 5
    otp_resend_seconds: int = 60
    cookie_secure: bool = True
    cors_origins: list[str] = []
    db_pool_size: int = 10
    db_statement_timeout_ms: int = 5000


@lru_cache
def get_settings() -> Settings:
    return Settings()
