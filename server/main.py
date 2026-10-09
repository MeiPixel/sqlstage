"""SqlStage 的 FastAPI 服务入口（工作台模式）。

工作台能连任意数据库、改配置、跑寻优，因此默认只听本机；一旦要对外，
必须设 SQLSTAGE_WORKBENCH_TOKEN，接口全部要求 Bearer Token。
"""
import os
import secrets

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .connections import router as connections_router
from .datasets import router as datasets_router
from .files import router as files_router
from .presets import router as presets_router
from .publish import router as publish_router
from .samples import router as samples_router
from .search import router as search_router
from .objects import router as objects_router
from .service_config import token_sha256

WORKBENCH_TOKEN_ENV = "SQLSTAGE_WORKBENCH_TOKEN"
bearer_scheme = HTTPBearer(auto_error=False)


def workbench_digest():
    """工作台访问令牌的 sha256；没设环境变量返回 None，表示不鉴权。

    只有显式设置环境变量才鉴权——本机开发是主要用法，
    每次启动都要求 token 会让「双击就能用」变成障碍。
    对外监听（--host 0.0.0.0）时必须设，否则任何人都能连任意库。
    """
    token = os.getenv(WORKBENCH_TOKEN_ENV, "")
    return token_sha256(token) if token else None


DIGEST = workbench_digest()


def require_token(credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme)):
    """没配环境变量就放行；配了则校验 Bearer Token，常量时间比对。"""
    if DIGEST is None:
        return
    if credentials is None:
        raise HTTPException(status_code=401, detail="缺少访问令牌")
    digest = token_sha256(credentials.credentials)
    if not secrets.compare_digest(digest, DIGEST):
        raise HTTPException(status_code=403, detail="令牌无效")


def create_app():
    app = FastAPI(title="SqlStage API")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    guard = [Depends(require_token)] if DIGEST else []
    for router in (connections_router, datasets_router, search_router, presets_router,
                   publish_router, samples_router, files_router, objects_router):
        app.include_router(router, dependencies=guard)

    @app.get("/api/health")
    def health():
        return {"status": "ok", "auth_required": DIGEST is not None}

    return app


app = create_app()
