import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_path: Path
    quote_ttl_seconds: int
    url_prefix: str

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.database_path.expanduser().resolve()}"


def load_settings() -> Settings:
    database_path = Path(os.getenv("PORTFOLIO_DB_PATH", "portfolio.db"))
    ttl = int(os.getenv("PORTFOLIO_QUOTE_TTL_SECONDS", "300"))
    if ttl < 0:
        raise ValueError("PORTFOLIO_QUOTE_TTL_SECONDS must be non-negative")
    url_prefix = os.getenv("PORTFOLIO_URL_PREFIX", "").rstrip("/")
    if url_prefix and not url_prefix.startswith("/"):
        raise ValueError("PORTFOLIO_URL_PREFIX must start with '/'")
    return Settings(database_path=database_path, quote_ttl_seconds=ttl, url_prefix=url_prefix)


settings = load_settings()
