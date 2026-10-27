from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_env_file(path: Path) -> None:
    """Load a small .env file without overriding values supplied by cron/systemd."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if value[:1] in {"'", '"'} and value[-1:] == value[:1]:
            value = value[1:-1]
        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class Config:
    project_root: Path
    data_dir: Path
    watchlist_path: Path
    scan_state_path: Path
    telegram_state_path: Path
    nse_symbols_path: Path
    et_rss_url: str
    telegram_bot_token: str
    telegram_owner_id: int | None
    gemini_api_key: str
    gemini_model: str
    telegram_getupdates_timeout: int
    initial_lookback_hours: int
    http_timeout_seconds: int

    @classmethod
    def load(cls, project_root: Path | None = None) -> "Config":
        root = (project_root or PROJECT_ROOT).resolve()
        load_env_file(root / ".env")
        data_dir_value = Path(os.getenv("STOCKPULSE_DATA_DIR", "data"))
        data_dir = data_dir_value if data_dir_value.is_absolute() else root / data_dir_value
        symbols_value = Path(os.getenv("NSE_SYMBOLS_CSV", str(data_dir / "nse_equity_symbols.csv")))
        symbols_path = symbols_value if symbols_value.is_absolute() else root / symbols_value
        owner_text = os.getenv("TELEGRAM_OWNER_ID", "").strip()
        try:
            owner_id = int(owner_text) if owner_text else None
        except ValueError as exc:
            raise ValueError("TELEGRAM_OWNER_ID must be a numeric Telegram user ID") from exc
        return cls(
            project_root=root,
            data_dir=data_dir,
            watchlist_path=data_dir / "watchlist.json",
            scan_state_path=data_dir / "scan_state.json",
            telegram_state_path=data_dir / "telegram_state.json",
            nse_symbols_path=symbols_path,
            et_rss_url=os.getenv(
                "ET_RSS_URL",
                "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
            ),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_owner_id=owner_id,
            gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip(),
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip(),
            telegram_getupdates_timeout=max(1, min(50, int(os.getenv("TELEGRAM_GETUPDATES_TIMEOUT", "50")))),
            initial_lookback_hours=max(1, int(os.getenv("INITIAL_LOOKBACK_HOURS", "18"))),
            http_timeout_seconds=max(5, int(os.getenv("HTTP_TIMEOUT_SECONDS", "30"))),
        )

    def require_telegram(self) -> None:
        if not self.telegram_bot_token:
            raise ValueError("Set TELEGRAM_BOT_TOKEN in .env")
        if self.telegram_owner_id is None:
            raise ValueError("Set TELEGRAM_OWNER_ID in .env")

    def require_scan(self) -> None:
        self.require_telegram()
        if not self.gemini_api_key:
            raise ValueError("Set GEMINI_API_KEY in .env")

    @property
    def telegram_access_path(self) -> Path:
        return self.data_dir / "telegram_access.json"

    def watchlist_path_for_user(self, user_id: int) -> Path:
        if user_id == self.telegram_owner_id:
            return self.watchlist_path
        return self.data_dir / "users" / str(user_id) / "watchlist.json"

    def scan_state_path_for_user(self, user_id: int) -> Path:
        if user_id == self.telegram_owner_id:
            return self.scan_state_path
        return self.data_dir / "users" / str(user_id) / "scan_state.json"

