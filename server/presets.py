"""ORM 预设方案：从 orm_presets/*.json 读取比赛等现成配置。"""
import json
from pathlib import Path

from fastapi import APIRouter

PRESET_DIR = Path(__file__).resolve().parents[1] / "orm_presets"

router = APIRouter()


@router.get("/api/orm/presets")
def list_presets():
    return [json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(PRESET_DIR.glob("*.json"))]
