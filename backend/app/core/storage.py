from __future__ import annotations

import os
from pathlib import Path


def resolve_temp_storage_root() -> Path:
    configured = os.getenv("TEMP_STORAGE_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    if os.getenv("VERCEL") == "1":
        return Path("/tmp/kibak")
    return Path(__file__).resolve().parents[2] / "storage"


def resolve_temp_storage_dir(*parts: str) -> Path:
    return resolve_temp_storage_root().joinpath(*parts)


def ensure_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path
