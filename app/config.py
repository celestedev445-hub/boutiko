from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", env_ignore_empty=True, protected_namespaces=()
    )

    database_url: str = "postgresql+psycopg://boutiko:boutiko@localhost:5432/boutiko"

    whatsapp_verify_token: str = ""
    whatsapp_access_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_app_secret: str = ""
    whatsapp_api_version: str = "v21.0"

    model_api_key: str = ""
    model_api_base_url: str = "https://api.meta.ai/v1"
    model_text: str = "muse-spark-1.3-contributor"


@lru_cache
def get_settings() -> Settings:
    return Settings()
