"""Explicit experimental integration entrypoint; app/main.py remains unchanged.

Usage: python -m uvicorn ml_tools.api:app --host 127.0.0.1 --port 8000
Start the separate local model first. This entrypoint is not jury-ready until
real-video acceptance and human rule applicability checks pass.
"""
import os
from pathlib import Path
import imageio_ffmpeg

from app.main import create_app

os.environ.setdefault('APP_FFMPEG_BIN', imageio_ffmpeg.get_ffmpeg_exe())
os.environ.setdefault('APP_ANALYZER_FACTORY', 'app.local_analyzer:build_analyzer')
os.environ.setdefault('APP_RULE_CATALOG_PATH', str(Path(__file__).resolve().parents[1] / 'app/data/ml-rules.json'))
app = create_app()
