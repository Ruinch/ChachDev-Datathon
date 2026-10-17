from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    api_key: str | None
    data_dir: Path
    max_upload_bytes: int = 128 * 1024 * 1024

    @classmethod
    def from_env(cls) -> "Settings":
        max_mb = int(os.getenv("APP_MAX_UPLOAD_MB", "128"))
        return cls(
            api_key=os.getenv("APP_API_KEY"),
            data_dir=Path(os.getenv("APP_DATA_DIR", ".local_data")),
            max_upload_bytes=max_mb * 1024 * 1024,
        )

    def validate(self) -> None:
        if not self.api_key:
            raise RuntimeError("APP_API_KEY must be set before starting the API")
        if self.max_upload_bytes <= 0:
            raise RuntimeError("APP_MAX_UPLOAD_MB must be positive")
