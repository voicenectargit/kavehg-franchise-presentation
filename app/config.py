"""
Configuration settings for Content Portal
"""
import logging
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)

    # App
    app_name: str = "Ambivo Content Portal"
    app_version: str = "1.0.0"
    debug: bool = False
    host: str = "0.0.0.0"
    port: int = 8003

    # JWT — must match ambivo_api cookie_secret
    jwt_secret_key: str
    jwt_algorithm: str = "HS256"

    # MongoDB
    mongodb_url: str
    mongodb_database: str = "omnilonely"

    # VectorDB API
    vectordb_api_url: str = "https://vectordbapi.ambivo.com"
    ambivo_internal_secret: str = ""

    # Core API (ambivo_api) — for login proxy
    ambivo_api_url: str = "https://goferapi.ambivo.com"

    # Chat limits
    daily_chat_limit: int = 20
    # Max chat answers streamed from the inference server at once (global).
    # Requests beyond this queue for a free slot instead of piling onto the
    # inferencing backend. Tune up/down to match backend capacity.
    max_concurrent_chats: int = 4

    # AI page generation (Anthropic)
    anthropic_api_key: str = ""
    ai_model: str = "claude-sonnet-4-20250514"

    # Logging
    log_level: str = "INFO"


settings = Settings()
logger.info(f"Settings loaded — DB: {settings.mongodb_database}, VectorDB: {settings.vectordb_api_url}")
