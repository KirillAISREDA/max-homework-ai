from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    gigachat_credentials: str = ""
    gigachat_scope: str = "GIGACHAT_API_PERS"
    gigachat_verify_ssl_certs: bool = True
    gigachat_ca_bundle: str | None = None
    # Дефолт SDK — 30 с без ретраев; vision-вызов легально живёт 30-60+ с (арх. §7)
    gigachat_timeout: float = 90.0
    gigachat_max_retries: int = 3
    # PERS-фримиум: 1 одновременный запрос; больше — 429 (арх. §8: семафор)
    gigachat_concurrency: int = 1

    max_token: str = ""
    max_base_url: str = "https://platform-api2.max.ru"

    # dev-события не попадают в конкурсные метрики (антифрод, Положение п. 2.2)
    environment: str = "dev"
    events_path: str = "var/events.jsonl"

    # Роутинг по моделям (арх. §4): Max — vision и сложная математика, Pro — тьютор,
    # Lite — короткие реплики. Идентификаторы сверять с актуальной линейкой GigaChat.
    vision_model: str = "GigaChat-2-Max"
    solver_model: str = "GigaChat-2-Max"
    tutor_model: str = "GigaChat-2-Pro"
    lite_model: str = "GigaChat-2"


def load_settings() -> Settings:
    return Settings()
