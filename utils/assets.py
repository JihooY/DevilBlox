from __future__ import annotations

from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
ASSETS_DIR = ROOT_DIR / "assets"


def asset_path(*parts: str) -> Path:
    return ASSETS_DIR.joinpath(*parts)


def has_asset(*parts: str) -> bool:
    return asset_path(*parts).is_file()
