r"""FastAPI 入口。

启动（工作目录必须是 backend/）：

    uv run python -m app.main        # uv 自动使用 backend/.venv

等价写法：

    .venv\Scripts\python -m app.main
    uvicorn app.main:app --host 127.0.0.1 --port 8010
"""

from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .api import ai as ai_api
from .api import articles, dictionary as dict_api, review as review_api, search, traces, vocab as vocab_api
from .config import settings
from .db import init_db
from .services import (
    ai as ai_service,
    audio as audio_service,
    dictdata,
    ecdict,
    ipa,
    jobs,
    srs,
    vocab,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
# httpx 每个请求都打一行，抓一篇图多的文章会刷屏
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings.ensure_dirs()
    init_db()
    logging.getLogger("english").info("数据目录 %s | 数据库 %s", settings.data_dir, settings.db_file)
    # 词典数据自检：缺什么补什么。**在后台线程里下**（ECDICT 207 MiB），
    # 所以这一行立刻返回 —— 起服务不该被一次下载堵住。状态在 /api/health 的 dict.bootstrap。
    dictdata.ensure_async()
    yield
    jobs.shutdown()


app = FastAPI(title="EnglishReadUp", version="0.1.0", lifespan=lifespan)

# 前端 Vite 开发服的跨源访问
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173", "http://127.0.0.1:5173",
        "http://localhost:4173", "http://127.0.0.1:4173",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(articles.router)
app.include_router(search.router)
app.include_router(traces.router)
app.include_router(dict_api.router)
app.include_router(ai_api.router)
app.include_router(vocab_api.router)
app.include_router(review_api.router)


@app.get("/media/{asset_key}/{filename}", include_in_schema=False)
def media(asset_key: str, filename: str) -> FileResponse:
    """本地化后的图片。文件名含内容哈希，可长期缓存。

    图片在抓取阶段已下载到本地，这里只做静态服务，
    因此不再需要 Referer —— 依赖原站的反而是原作者。
    """
    if not SAFE_NAME.match(asset_key) or not SAFE_NAME.match(filename):
        raise HTTPException(status_code=404, detail="路径不合法")
    base = settings.assets_dir.resolve()
    path = (base / asset_key / filename).resolve()
    if base not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="图片不存在")
    return FileResponse(
        path, headers={"Cache-Control": "public, max-age=31536000, immutable"}
    )


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "data_dir": str(settings.data_dir),
        "db": str(settings.db_file),
        "fetching": sorted(jobs.in_flight()),
        "render_domains": sorted(settings.render_domain_set),
        # 只暴露「配没配」，绝不回显 key 本身
        "llm": {
            "provider": settings.llm_provider,
            "model": settings.llm_model,
            "api_key_set": bool(settings.llm_api_key),
            "prompt_version": ai_service.PROMPT_VERSION,
        },
        # 词典：划词的音标与释义都来自这两个**本地文件**（红线 3），
        # 这里只报「在不在、多大」，**不做 COUNT(*)** —— 340 万行的计数
        # 会把健康检查从毫秒拖成秒级，而那个数字只有安装词库时才有意义。
        "dict": {
            "ecdict": ecdict.status(),
            "ipa": ipa.status(),
            # 启动自检 / 自动补齐的进度：state、missing、当前数据集与百分比。
            # 数据齐备时就是一个 state=ready，别指望它在这里告诉你词条数。
            "bootstrap": dictdata.status(),
        },
        # 前端产物是否由本进程托管：true 时直接开 http://127.0.0.1:8010 就是界面，
        # 不必再单起 Vite。false 就去 npm run build（或把 dist 指对）。
        "frontend": {"served": frontend_served(), "dist": str(FRONTEND_DIST)},
        # 发音：源、默认口音与本地化进度。音频是**点喇叭才下载**的，所以
        # cached/pending 是「用户点过多少」的意思，不是预热进度。
        # breaker=true 说明有道那边被熔断保护了，此时会退回词典录音。
        "audio": {
            "provider": "youdao" if settings.audio_enabled else "off",
            "accent": settings.audio_accent,
            "_fallback_records_from": settings.dict_api_base,
            **audio_service.stats(),
        },
        # M4：一眼看出生词本有没有东西，验收脚本用它判断入册是否生效
        "vocab": vocab.stats(),
        # M5：复习队列欠了多少张 —— 导航徽标与验收脚本都读它
        "review": {"due_now": srs.stats()["due_now"]},
    }


# 这两个前缀永远走接口，不能被 SPA 回退吞掉
API_PREFIXES = {"api", "media"}
FRONTEND_DIST = settings.dist_dir


class _HashedAssets(StaticFiles):
    """Vite 产物文件名带内容哈希 → 可以永久缓存。

    与 index.html 的 `no-cache` 正好组成正确的组合：壳子每次校验，资源一次拿够。
    """

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        if resp.status_code == 200:
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp


def frontend_served() -> bool:
    return settings.serve_frontend and (FRONTEND_DIST / "index.html").is_file()


def mount_frontend() -> None:
    """把前端构建产物挂到后端，省掉 Vite 那个常驻服务。

    必须放在所有路由注册之后 —— Starlette 按注册顺序匹配，catch-all 一提前，
    `/api/health` 之类的精确路由就永远轮不到了。

    开发时照旧 `cd frontend && npm run dev`（5173 代理到 8010），两条路并存；
    这里是给「自己用一个服务就够」的场景用的。
    """
    logger = logging.getLogger("english")
    if not settings.serve_frontend:
        logger.info("SERVE_FRONTEND=false，不托管前端产物")
        return
    if not (FRONTEND_DIST / "index.html").is_file():
        logger.warning(
            "未找到 %s/index.html，跳过前端托管（cd frontend && npm run build 后重启）",
            FRONTEND_DIST,
        )
        return

    assets = FRONTEND_DIST / "assets"
    if assets.is_dir():
        app.mount("/assets", _HashedAssets(directory=assets), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> FileResponse:
        """SPA 回退：命中真实文件就发文件，其余交给 index.html 由 react-router 接管。

        `/a/3?block=12&start=..&end=..` 这类深链必须能直接刷新，所以不能在这里 404。
        但两种请求要显式排除：

        - `/api`、`/media` 前缀：打错的接口路径若返回 HTML，前端 `res.json()` 会抛
          "Unexpected token '<'"，比 404 难查得多。
        - 带扩展名的路径：`favicon.ico`、`robots.txt` 这类确实缺文件时塞一份 HTML
          回去只会让浏览器解析失败，404 更诚实（真存在于 dist 的照发，见上）。
        """
        if path.split("/", 1)[0] in API_PREFIXES:
            raise HTTPException(status_code=404, detail="接口不存在")
        target = (FRONTEND_DIST / path).resolve()
        # 子路径校验，防 `../../` 穿出 dist
        if path and FRONTEND_DIST in target.parents and target.is_file():
            return FileResponse(target)
        if path and "." in path.rsplit("/", 1)[-1]:
            raise HTTPException(status_code=404, detail="静态资源不存在")
        # index.html 绝不能缓存，否则换了版本浏览器还在跑旧壳子
        return FileResponse(FRONTEND_DIST / "index.html", headers={"Cache-Control": "no-cache"})

    logger.info("前端产物已托管：%s", FRONTEND_DIST)


def _dev() -> None:  # pragma: no cover
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=8010, reload=False)


mount_frontend()


if __name__ == "__main__":  # pragma: no cover
    _dev()
