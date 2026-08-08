import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_path: Path
    quote_ttl_seconds: int
    url_prefix: str
    redis_url: str | None
    history_cache_ttl_seconds: int
    history_recent_ttl_seconds: int

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
    # Unset means no cache at all: every request goes to the provider, exactly as before.
    redis_url = os.getenv("PORTFOLIO_REDIS_URL") or None
    history_ttl = int(os.getenv("PORTFOLIO_HISTORY_CACHE_TTL_SECONDS", str(30 * 24 * 3600)))
    recent_ttl = int(os.getenv("PORTFOLIO_HISTORY_RECENT_TTL_SECONDS", "600"))
    if history_ttl < 0 or recent_ttl < 0:
        raise ValueError("history cache TTLs must be non-negative")
    return Settings(
        database_path=database_path,
        quote_ttl_seconds=ttl,
        url_prefix=url_prefix,
        redis_url=redis_url,
        history_cache_ttl_seconds=history_ttl,
        history_recent_ttl_seconds=recent_ttl,
    )


settings = load_settings()
