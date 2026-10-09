"""产物下载：只允许读取 runs/ 与 examples/ 下的文件。

寻优排行榜、评测结果这类产物以磁盘路径返回给前端，前端无法直接读取服务端磁盘，
所以单独开一个带白名单的下载端点。路径必须先解析再比对，避免 ../ 穿越。
"""
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

ROOT = Path(__file__).resolve().parents[1]
ALLOWED = (ROOT / "runs", ROOT / "examples")

MEDIA_TYPES = {".csv": "text/csv", ".json": "application/json", ".jsonl": "application/jsonl",
               ".md": "text/markdown", ".txt": "text/plain", ".sqlite": "application/octet-stream"}

router = APIRouter()


@router.get("/api/files/download")
def download(path: str = Query(..., description="runs/ 或 examples/ 下的文件绝对路径")):
    try:
        resolved = Path(path).resolve()
    except (OSError, ValueError):
        raise HTTPException(status_code=400, detail="路径无法解析")
    if not any(resolved.is_relative_to(base) for base in ALLOWED):
        raise HTTPException(status_code=403, detail="只允许下载 runs/ 与 examples/ 下的文件")
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(resolved, filename=resolved.name,
                        media_type=MEDIA_TYPES.get(resolved.suffix.lower(), "application/octet-stream"))
