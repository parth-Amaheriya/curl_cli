from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
import secrets
from typing import List


class Settings(BaseSettings):
    """Application settings loaded from environment variables"""

    # App
    app_name: str = "Curl to Python Converter API"
    app_version: str = "2.0.0"
    debug: bool = False

    # MongoDB
    mongodb_url: str = "mongodb://localhost:27017"
    mongodb_db_name: str = "curl_converter_db"

    # JWT
    jwt_secret_key: str = Field(default_factory=lambda: secrets.token_urlsafe(32))
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_minutes: int = 1440

    # Security
    bcrypt_rounds: int = 12

    # CORS (important fix: must be typed correctly)
    cors_origins: List[str] = ["*"]

    # Rate limiting
    rate_limit_per_minute: int = 60

    # Logging
    log_level: str = "INFO"

    # IMPORTANT: Pydantic v2 config
    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=False,
        extra="ignore"   # 🔥 FIXES "Extra inputs are not permitted"
    )


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()