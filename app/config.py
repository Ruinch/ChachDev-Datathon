from __future__ import annotations

import os
import math
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    api_key: str | None
    data_dir: Path
    max_upload_bytes: int = 128 * 1024 * 1024
    max_video_duration_sec: float = 180
    job_timeout_sec: float = 240
    probe_timeout_sec: float = 15
    upload_timeout_sec: float = 30
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"
    analyzer_factory: str | None = None
    rule_catalog_path: Path | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        max_mb = int(os.getenv("APP_MAX_UPLOAD_MB", "128"))
        rules_path = os.getenv("APP_RULE_CATALOG_PATH")
        return cls(
            api_key=os.getenv("APP_API_KEY"),
            data_dir=Path(os.getenv("APP_DATA_DIR", ".local_data")),
            max_upload_bytes=max_mb * 1024 * 1024,
            max_video_duration_sec=float(os.getenv("APP_MAX_VIDEO_SECONDS", "180")),
            job_timeout_sec=float(os.getenv("APP_ANALYSIS_TIMEOUT_SECONDS", "240")),
            probe_timeout_sec=float(os.getenv("APP_FFPROBE_TIMEOUT_SECONDS", "15")),
            upload_timeout_sec=float(os.getenv("APP_UPLOAD_TIMEOUT_SECONDS", "30")),
            ffmpeg_bin=os.getenv("APP_FFMPEG_BIN", "ffmpeg"),
            ffprobe_bin=os.getenv("APP_FFPROBE_BIN", "ffprobe"),
            analyzer_factory=os.getenv("APP_ANALYZER_FACTORY") or None,
            rule_catalog_path=Path(rules_path) if rules_path else None,
        )

    def validate(self) -> None:
        if not self.api_key:
            raise RuntimeError("APP_API_KEY must be set before starting the API")
        if self.max_upload_bytes <= 0:
            raise RuntimeError("APP_MAX_UPLOAD_MB must be positive")
        for name, value in (
            ("APP_MAX_VIDEO_SECONDS", self.max_video_duration_sec),
            ("APP_ANALYSIS_TIMEOUT_SECONDS", self.job_timeout_sec),
            ("APP_FFPROBE_TIMEOUT_SECONDS", self.probe_timeout_sec),
            ("APP_UPLOAD_TIMEOUT_SECONDS", self.upload_timeout_sec),
        ):
            if not math.isfinite(value) or value <= 0:
                raise RuntimeError(f"{name} must be a finite positive number")
        if self.job_timeout_sec > 270:
            raise RuntimeError("APP_ANALYSIS_TIMEOUT_SECONDS must not exceed 270")
        if self.upload_timeout_sec + self.probe_timeout_sec + self.job_timeout_sec > 295:
            raise RuntimeError("Upload, probe and analysis timeouts must total at most 295 seconds")
        if self.analyzer_factory and not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*", self.analyzer_factory):
            raise RuntimeError("APP_ANALYZER_FACTORY must use module:factory syntax")
