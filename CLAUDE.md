# B站历史评论查询 — AI 速读指南

## 项目概述

Web 应用，查询 B站用户的评论历史。两种模式：
1. **按 UID 查询**：扫某 UP 主视频评论区，匹配当前登录用户留下的评论
2. **查询我的评论**：拉消息中心回复通知，提取原始评论

## 技术栈

- **后端**：Python FastAPI + uvicorn，SSE 流式推送进度
- **前端**：原生 HTML/JS，无框架，Jinja2 渲染
- **HTTP**：`curl_cffi` 模拟 Chrome 120 TLS 指纹，绕过 B站风控
- **签名**：B站 WBI 签名（`_sign_params`），用于空间视频列表 API

## 文件结构

```
bilibili-comments/
├── main.py              # FastAPI 服务端，路由 + 后台线程
├── bilibili_api.py      # BilibiliClient 类，封装所有 B站 API
├── templates/
│   └── index.html       # 前端单页，搜索框 + 进度条 + 结果表格
├── requirements.txt     # 依赖
└── CLAUDE.md            # 本文
```

## BilibiliClient（bilibili_api.py）

核心客户端，每个实例持有独立 `curl_cffi.Session`。

### 初始化流程
```
__init__(cookie_str=None)
  → _load_cookies(cookie_str)     # 支持传参或读环境变量
  → session(impersonate="chrome120")

首次 API 调用时自动触发 _ensure_session():
  → GET www.bilibili.com（首页，种 Cookie）
  → GET /x/web-interface/nav（获取 wbi_img + 登录态 mid）
  → 存 self._mixin_key（WBI 签名用）、self._my_uid（登录用户 UID）
```

### 关键方法

| 方法 | API | 说明 |
|------|-----|------|
| `get_user_info(uid)` | `x/space/acc/info` | 用户基本信息 |
| `get_user_videos(uid, page)` | `x/space/wbi/arc/search` | 视频列表（WBI 签名） |
| `get_video_comments(aid, page)` | `x/v2/reply` | 视频评论，每页 20 条 |
| `scan_user_comments(uid, depth)` | 上述组合 | 扫 UP 主视频→匹配登录用户评论 |
| `get_my_comment_history()` | `x/msgfeed/reply?ps=320` | 消息中心评论历史 |

### 反爬措施
- `_request(url)`：自动重试 HTTP 412 / JSON -799 -412 -401，退避 5s
- `impersonate="chrome120"`：TLS 指纹模拟
- 扫描视频间延迟 0.2~0.5s，评论页间 0.1~0.3s
- WBI 签名密钥过期时自动刷新（`_keys_refreshed` 防死循环）

## FastAPI 路由（main.py）

| 路由 | 说明 |
|------|------|
| `GET /` | 页面 |
| `GET /api/user/{uid}/info` | 用户信息 |
| `GET /api/scan/{uid}?depth=&cookie=` | 启动扫描（后台线程） |
| `GET /api/my-comments?cookie=` | 查我的评论（后台线程） |
| `GET /api/task/{task_id}/events` | SSE 进度推送 |

### 线程模型
- 共享客户端：`_get_client()` 线程安全单例（用于无 cookie 参数时）
- 有 cookie 参数时：`BilibiliClient(cookie_str=cookie)` 独立实例
- 后台线程：`_run_scan` / `_run_my_scan`，通过 `scan_tasks[task_id]` 共享状态
- SSE 轮询 0.5s 读取任务状态，完成时推送完整结果

## 前端关键逻辑（index.html）

- Cookie 输入框（SESSDATA/bili_jct/buvid3），`localStorage` 持久化
- `startScan()`：按 UID 查询，调 `/api/scan/{uid}` + SSE
- `startMyScan()`：查我的评论，调 `/api/my-comments` + SSE
- SSE 收到 `progress` 事件更新进度条，`complete` 事件渲染结果表格
- 结果支持正序/倒序排序，前端分页（每页 20 条）

## 运行

```bash
cd bilibili-comments
pip install fastapi uvicorn curl_cffi jinja2
python main.py
# 访问 http://127.0.0.1:8000
```

Cookie 获取方式：浏览器 F12 → 任意 bilibili.com 请求 → 复制 Cookie 请求头中的 SESSDATA 值，粘贴到页面输入框。

## 已知限制

- `get_my_comment_history()` 只能查到有人回复过的评论（消息中心 API 限制）
- `scan_user_comments()` 需要知道目标 UP 主 UID，且每视频只扫 2 页评论
- B站 `x/msgfeed/reply` 翻页参数（cursor/pn）无效，`ps=320` 单次拉取约 112 条
- 风控返回 412 时需等待冷却，无法绕过
