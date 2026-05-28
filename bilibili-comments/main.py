"""
B站历史评论查询 — FastAPI 服务端
"""

import asyncio
import json
import threading
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
import uvicorn

from bilibili_api import BilibiliClient

app = FastAPI(title="B站历史评论查询")
templates = Jinja2Templates(directory="templates")

# 后台扫描任务存储
# { task_id: { "lock": Lock, "status": ..., "comments": [...], ... } }
scan_tasks: dict = {}

# 全局共享的 BilibiliClient（复用 Session，避免每次新建触发风控）
_shared_client: BilibiliClient | None = None
_client_lock = threading.Lock()


def _get_client() -> BilibiliClient:
    """获取共享的 BilibiliClient（线程安全）"""
    global _shared_client
    if _shared_client is None:
        with _client_lock:
            if _shared_client is None:
                _shared_client = BilibiliClient()
                _shared_client._ensure_session()
    return _shared_client


@app.on_event("startup")
async def on_startup():
    """启动时检查 Cookie 配置状态"""
    import os
    has_cookie = any(
        os.environ.get(k)
        for k in ["BILIBILI_COOKIE", "BILIBILI_SESSDATA", "BILIBILI_BUVID3"]
    )
    if has_cookie:
        print("[配置] 已加载 B站 Cookie")
    else:
        print("[配置] 未设置 Cookie，可能触发风控限流")
        print("  建议设置环境变量 BILIBILI_COOKIE 或 BILIBILI_SESSDATA/BILIBILI_BUVID3")


# ======================== 页面路由 ========================


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


# ======================== API 路由 ========================


@app.get("/api/user/{uid}/info")
async def api_user_info(uid: int):
    """获取用户基本信息"""
    client = _get_client()
    info = client.get_user_info(uid)
    if info is None:
        raise HTTPException(status_code=404, detail="用户不存在或获取失败")
    stat = client.get_user_stats(uid)
    if stat:
        info["follower"] = stat.get("follower", 0)
        info["video_count"] = stat.get("video", 0)
    return {"code": 0, "data": info}


@app.get("/api/scan/{uid}")
async def api_start_scan(uid: int, depth: int = 2):
    """启动历史评论扫描，返回 task_id"""
    client = _get_client()
    # 首次调用可能触发风控，加重试
    info = None
    for attempt in range(3):
        info = client.get_user_info(uid)
        if info is not None:
            break
        await asyncio.sleep((attempt + 1) * 2)
    if info is None:
        raise HTTPException(status_code=400, detail="获取用户信息失败，请检查 UID")
    stat = client.get_user_stats(uid)
    user_info = {
        "name": info.get("name", str(uid)),
        "face": info.get("face", ""),
        "level": info.get("level", 0),
        "sign": info.get("sign", ""),
        "follower": stat.get("follower", 0) if stat else 0,
        "video_count": stat.get("video", 0) if stat else 0,
    }

    task_id = uuid.uuid4().hex[:12]
    scan_tasks[task_id] = {
        "lock": threading.Lock(),
        "status": "pending",
        "user_info": user_info,
        "comments": [],
        "stats": {},
        "error": None,
    }

    thread = threading.Thread(
        target=_run_scan, args=(task_id, uid, depth, user_info), daemon=True
    )
    thread.start()

    return {"task_id": task_id}


@app.get("/api/my-comments")
async def api_my_comments():
    """查询当前账号的评论历史（通过消息中心回复通知）"""
    client = _get_client()

    task_id = uuid.uuid4().hex[:12]
    scan_tasks[task_id] = {
        "lock": threading.Lock(),
        "status": "pending",
        "user_info": {"name": "当前账号", "face": "", "level": 0, "sign": "", "follower": 0, "video_count": 0},
        "comments": [],
        "stats": {},
        "error": None,
    }

    thread = threading.Thread(
        target=_run_my_scan, args=(task_id,), daemon=True
    )
    thread.start()

    return {"task_id": task_id}


@app.get("/api/task/{task_id}/events")
async def api_task_events(task_id: str):
    """SSE 事件流 — 实时推送扫描进度与结果"""
    task = scan_tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    async def event_generator():
        try:
            while True:
                with task["lock"]:
                    status = task["status"]
                    stats = task.get("stats", {}).copy()
                    # 只发轻量进度
                    data = {
                        "status": status,
                        "video_scanned": stats.get("video_scanned", 0),
                        "total_videos": stats.get("total_videos", 0),
                        "comments_found": stats.get("comments_found", len(task["comments"])),
                        "current_video": stats.get("current_video", ""),
                        "phase": stats.get("phase", ""),
                        "user_name": stats.get("user_name", ""),
                    }

                if status == "completed":
                    # 发送完整结果
                    with task["lock"]:
                        result = {
                            "status": "completed",
                            "comments": task["comments"],
                            "user_info": task["user_info"],
                            "stats": {
                                "video_scanned": stats.get("video_scanned", 0),
                                "total_videos": stats.get("total_videos", 0),
                                "comments_found": stats.get("comments_found", len(task["comments"])),
                                "pages_scanned": stats.get("pages_scanned", 0),
                            },
                        }
                    yield f"event: complete\ndata: {json.dumps(result, ensure_ascii=False)}\n\n"
                    return

                if status == "error":
                    with task["lock"]:
                        err = task.get("error", "未知错误")
                    yield f"event: error\ndata: {json.dumps({'error': err}, ensure_ascii=False)}\n\n"
                    return

                # 进度事件
                yield f"event: progress\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/comment/{rpid}/delete")
async def api_delete_comment(rpid: int, aid: int, cookie: str = ""):
    """删除指定评论（需 bili_jct）"""
    if not cookie:
        raise HTTPException(status_code=400, detail="需要提供 Cookie（bili_jct 必填）")
    client = BilibiliClient(cookie_str=cookie)
    result = client.delete_comment(aid=aid, rpid=rpid)
    code = result.get("code", -1)
    if code != 0:
        msg = result.get("message", f"B站返回: code={code}")
        raise HTTPException(status_code=400, detail=msg)
    return {"code": 0, "message": "已删除"}


# ======================== 后台扫描 ========================


def _run_scan(task_id: str, uid: int, depth: int, user_info: dict):
    """在后台线程中执行扫描"""
    task = scan_tasks[task_id]

    def progress_callback(stats: dict):
        """被 bilibili_api 回调更新进度"""
        with task["lock"]:
            task["stats"] = stats

    try:
        with task["lock"]:
            task["status"] = "scanning"

        client = _get_client()
        comments, stats = client.scan_user_comments(
            uid=uid,
            depth=depth,
            user_info=user_info,
            max_comment_pages=2,
            progress_callback=progress_callback,
        )

        with task["lock"]:
            task["status"] = "completed"
            task["comments"] = comments
            task["user_info"] = user_info
            task["stats"] = stats

    except Exception as e:
        with task["lock"]:
            task["status"] = "error"
            task["error"] = str(e)


def _run_my_scan(task_id: str):
    """在后台线程中执行评论历史查询"""
    task = scan_tasks[task_id]

    def progress_callback(stats: dict):
        """被 bilibili_api 回调更新进度"""
        with task["lock"]:
            task["stats"] = {
                "video_scanned": stats.get("pages_scanned", 0),
                "total_videos": stats.get("total_pages", 0),
                "comments_found": stats.get("comments_found", 0),
                "current_video": "",
                "phase": stats.get("phase", ""),
                "user_name": "当前账号",
            }

    try:
        with task["lock"]:
            task["status"] = "scanning"

        client = _get_client()
        comments, stats = client.get_my_comment_history(
            progress_callback=progress_callback,
        )

        with task["lock"]:
            task["status"] = "completed"
            task["comments"] = comments
            task["stats"] = {
                "video_scanned": stats.get("pages_scanned", 0),
                "total_videos": stats.get("total_pages", 0),
                "comments_found": stats.get("comments_found", 0),
                "pages_scanned": stats.get("pages_scanned", 0),
            }

    except Exception as e:
        with task["lock"]:
            task["status"] = "error"
            task["error"] = str(e)


# ======================== 入口 ========================


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
