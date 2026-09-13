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
    # platform-api2.max.ru подписан НУЦ Минцифры: корень добавляется к certifi
    max_ca_bundle: str | None = None

    # dev-события не попадают в конкурсные метрики (антифрод, Положение п. 2.2)
    environment: str = "dev"
    events_path: str = "var/events.jsonl"
    # обезличенные id тестеров (поле "user" в events.jsonl) через запятую: в prod их
    # события пишутся с env=test и не попадают в зачёт
    test_users: str = ""

    # состояние диалога: пусто — в памяти процесса (локально), иначе Redis (сервер)
    redis_url: str | None = None
    # фото домашек для разбора спорных проверок; 0 — не сохранять
    photos_dir: str = "var/photos"
    photos_ttl_days: int = 30
    # лог бота в файле (ротация): логи контейнера пропадают при пересборке; пусто — только stderr
    log_path: str | None = None

    # Роутинг по моделям (арх. §4): Max — vision и сложная математика, Pro — тьютор,
    # Lite — короткие реплики. Идентификаторы сверять с актуальной линейкой GigaChat.
    vision_model: str = "GigaChat-2-Max"
    solver_model: str = "GigaChat-2-Max"
    tutor_model: str = "GigaChat-2-Pro"
    lite_model: str = "GigaChat-2"

    @property
    def test_user_hashes(self) -> frozenset[str]:
        return frozenset(u.strip() for u in self.test_users.split(",") if u.strip())


def load_settings() -> Settings:
    return Settings()
