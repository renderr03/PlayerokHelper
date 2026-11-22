from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet
from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    token: str
    admins: tuple[int, ...]
    kosell_key: str
    kosell_url: str
    session_file: Path
    encryption_key: str
    data_dir: Path
    timezone: str = "Europe/Moscow"
    poll_seconds: int = 15
    visibility_file: Path = Path("secrets/visibility.json")

    @classmethod
    def load(cls) -> Config:
        load_dotenv(interpolate=False)
        result = cls(
            token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            admins=tuple(int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()),
            kosell_key=os.getenv("KOSELL_API_KEY", ""),
            kosell_url=os.getenv("KOSELL_BASE_URL", "https://ru.kosell.store").rstrip("/"),
            session_file=Path(os.getenv("PLAYEROK_SESSION_FILE", "secrets/playerok.json")),
            encryption_key=os.getenv("STOCK_ENCRYPTION_KEY", ""),
            data_dir=Path(os.getenv("DATA_DIR", "data")),
            timezone=os.getenv("TIMEZONE", "Europe/Moscow"),
            poll_seconds=max(5, int(os.getenv("POLL_SECONDS", "15"))),
            visibility_file=Path(os.getenv("PLAYEROK_VISIBILITY_CONTRACT", "secrets/visibility.json")),
        )
        ZoneInfo(result.timezone)
        return result

    def validate(self) -> None:
        if not self.admins or not self.token or not self.encryption_key:
            raise ValueError("Заполните TELEGRAM_BOT_TOKEN, ADMIN_IDS и STOCK_ENCRYPTION_KEY в .env")
        Fernet(self.encryption_key.encode())
