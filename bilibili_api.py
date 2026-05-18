"""
Bilibili API 客户端 — 封装 WBI 签名、用户信息、视频列表、评论获取

Cookie 配置（环境变量）：
  BILIBILI_COOKIE  完整的 Cookie 字符串（从浏览器复制）
  或分别设置：
    BILIBILI_SESSDATA  SESSDATA 的值
    BILIBILI_BILI_JCT  bili_jct 的值
    BILIBILI_BUVID3    buvid3 的值
"""

import hashlib
import os
import random
import re
import time
import urllib.parse

from curl_cffi import requests

MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0"
    ),
    "Referer": "https://www.bilibili.com/",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,ja;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Ch-Ua": '"Chromium";v="125", "Microsoft Edge";v="125", "Not.A/Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
}


class BilibiliClient:
    """每个实例持有一个独立的 curl_cffi Session"""

    def __init__(self):
        self.session = requests.Session(impersonate="chrome120")
        self.session.headers.update(HEADERS)
        self._mixin_key = None
        self._keys_refreshed = False  # 防死循环
        self._session_ready = False
        self._load_cookies()

    def _build_cookie_str(self) -> str:
        """从环境变量构建 Cookie 字符串"""
        # 优先使用完整 Cookie 字符串
        cookie_str = os.environ.get("BILIBILI_COOKIE", "").strip()
        if cookie_str:
            return cookie_str

        # 分别设置
        mapping = [
            ("BILIBILI_SESSDATA", "SESSDATA"),
            ("BILIBILI_BILI_JCT", "bili_jct"),
            ("BILIBILI_BUVID3", "buvid3"),
            ("BILIBILI_BUVID4", "buvid4"),
        ]
        parts = []
        for env_key, cookie_key in mapping:
            val = os.environ.get(env_key, "").strip()
            if val:
                parts.append(f"{cookie_key}={val}")
        return "; ".join(parts)

    def _load_cookies(self):
        """将环境变量中的 Cookie 注入到 Session 的 CookieJar 中"""
        cookie_str = self._build_cookie_str()
        if not cookie_str:
            return

        for item in cookie_str.split(";"):
            item = item.strip()
            if "=" not in item:
                continue
            k, v = item.split("=", 1)
            k, v = k.strip(), v.strip()
            if not k or not v:
                continue
            try:
                self.session.cookies.set(k, v, domain=".bilibili.com")
            except Exception:
                try:
                    self.session.cookies.set(k, v)
                except Exception:
                    pass

    # ---------- Session 初始化 + WBI 签名 ----------

    def _ensure_session(self):
        """初始化 Session：访问首页 → nav API → WBI 密钥"""
        if self._session_ready:
            return

        # 1. 访问首页（有 Cookie 则自动携带）
        self.session.headers["Accept"] = (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8"
        )
        self.session.get("https://www.bilibili.com/", timeout=10)
        self.session.headers["Accept"] = "application/json, text/plain, */*"

        # 2. 调用 nav API 获取 WBI 密钥
        #    即使返回 -101（未登录），data 中仍然包含 wbi_img
        for attempt in range(3):
            resp = self.session.get(
                "https://api.bilibili.com/x/web-interface/nav", timeout=10
            )
            data = resp.json()

            wbi = data.get("data")
            if wbi and "wbi_img" in wbi:
                img_url = wbi["wbi_img"]["img_url"]
                sub_url = wbi["wbi_img"]["sub_url"]
                img_key = img_url.rsplit("/", 1)[-1].split(".")[0]
                sub_key = sub_url.rsplit("/", 1)[-1].split(".")[0]
                self._mixin_key = self._get_mixin_key(img_key, sub_key)
                self._session_ready = True
                return

            # 限流才重试
            if data.get("code") in (-799, -412):
                time.sleep((attempt + 1) * 5)
                continue

            raise RuntimeError(
                f"nav API 初始化失败: code={data.get('code')} msg={data.get('message')}"
            )

        raise RuntimeError("nav API 初始化失败: 重试耗尽")

    def _get_mixin_key(self, img_key: str, sub_key: str) -> str:
        raw = img_key + sub_key
        return "".join(raw[i] for i in MIXIN_KEY_ENC_TAB)[:32]

    def _ensure_wbi(self):
        """确保 WBI 密钥已加载"""
        self._ensure_session()
        if self._mixin_key is None:
            raise RuntimeError("WBI 密钥加载失败")

    def _sign_params(self, params: dict) -> str:
        """为空间视频列表 API 生成带 WBI 签名的 URL"""
        self._ensure_wbi()
        p = params.copy()

        p["web_location"] = 1550101
        chars = "ABCDEFGHIJK"
        p["dm_img_list"] = "[]"
        p["dm_img_str"] = "".join(random.sample(chars, 2))
        p["dm_cover_img_str"] = "".join(random.sample(chars, 2))
        p["dm_img_inter"] = '{"ds":[],"wh":[0,0,0],"of":[0,0,0]}'
        p["wts"] = int(time.time())

        def esc(s):
            return "".join(ch for ch in str(s) if ch not in "!'()*")

        cleaned = {esc(k): esc(v) for k, v in sorted(p.items())}
        parts = []
        for k, v in cleaned.items():
            ek = urllib.parse.quote(k, safe="!'()*")
            ev = urllib.parse.quote(v, safe="!'()*")
            ek = re.sub(r"%([0-9a-f]{2})", lambda m: "%" + m.group(1).upper(), ek)
            ev = re.sub(r"%([0-9a-f]{2})", lambda m: "%" + m.group(1).upper(), ev)
            parts.append(f"{ek}={ev}")

        query = "&".join(parts)
        w_rid = hashlib.md5((query + self._mixin_key).encode("utf-8")).hexdigest()
        p["w_rid"] = w_rid

        q = urllib.parse.urlencode(p, safe="!'()*")
        return f"https://api.bilibili.com/x/space/wbi/arc/search?{q}"

    def _request(self, url: str, max_retries=3):
        """带反爬重试的 GET 请求，处理 HTTP 412、JSON -799/-412/-401、网络超时"""
        last_err = None
        for attempt in range(max_retries):
            try:
                resp = self.session.get(url, timeout=15)
            except Exception as e:
                last_err = e
                wait = (attempt + 1) * 5
                time.sleep(wait)
                continue

            # HTTP 级别拦截
            if resp.status_code == 412:
                wait = (attempt + 1) * 5
                time.sleep(wait)
                continue

            # JSON 级别限流 / 风控
            try:
                data = resp.json()
                code = data.get("code", 0)
                if code in (-799, -412, -401):
                    wait = (attempt + 1) * 5
                    time.sleep(wait)
                    continue
            except Exception:
                pass

            return resp

        return None  # 重试耗尽

    # ---------- 用户信息 ----------

    def get_user_info(self, uid: int) -> dict | None:
        """获取用户基本信息"""
        self._ensure_session()
        resp = self._request(
            f"https://api.bilibili.com/x/space/acc/info?mid={uid}"
        )
        if resp is None:
            return None
        data = resp.json()
        if data.get("code") != 0:
            return None
        return data["data"]

    def get_user_stats(self, uid: int) -> dict | None:
        """获取粉丝/视频数统计"""
        self._ensure_session()
        resp = self._request(
            f"https://api.bilibili.com/x/relation/stat?vmid={uid}"
        )
        if resp is None:
            return None
        data = resp.json()
        if data.get("code") != 0:
            return None
        return data["data"]

    # ---------- 视频列表 ----------

    def get_user_videos(self, uid: int, page: int = 1, page_size: int = 50) -> list:
        """获取用户视频列表（第 page 页），返回 vlist"""
        url = self._sign_params({
            "mid": uid, "ps": page_size, "pn": page, "order": "pubdate"
        })

        resp = self._request(url)
        if resp is None:
            return []

        data = resp.json()
        if data.get("code") == -352 and not self._keys_refreshed:
            # WBI 过期，刷新密钥
            self._keys_refreshed = True
            self._mixin_key = None
            return self.get_user_videos(uid, page, page_size)

        if data.get("code") != 0:
            return []

        return data["data"]["list"]["vlist"]

    def get_total_video_count(self, uid: int) -> int:
        """获取用户视频总数"""
        url = self._sign_params({"mid": uid, "ps": 1, "pn": 1, "order": "pubdate"})
        resp = self._request(url)
        if resp is None:
            return 0
        data = resp.json()
        if data.get("code") != 0:
            return 0
        return data["data"]["page"]["count"]

    # ---------- 评论 ----------

    def get_video_comments(self, aid: int, page: int = 1) -> list:
        """
        获取视频第 page 页评论。
        尝试用 x/v2/reply（分页模式），返回 replies 列表。
        """
        self._ensure_session()
        url = (
            f"https://api.bilibili.com/x/v2/reply"
            f"?type=1&oid={aid}&pn={page}&ps=20&sort=2"
        )
        resp = self._request(url)
        if resp is None:
            return []

        data = resp.json()
        if data.get("code") != 0:
            return []

        replies = data["data"].get("replies")
        return replies if replies is not None else []

    # ---------- 扫描编排 ----------

    def scan_user_comments(
        self,
        uid: int,
        depth: int = 2,
        user_info: dict | None = None,
        max_comment_pages: int = 5,
        progress_callback=None,
    ):
        """
        扫描用户的历史评论。

        Parameters
        ----------
        uid : int
            目标用户 UID
        depth : int
            扫描视频列表的页数（每页 50 个视频）
        user_info : dict or None
            已获取的用户信息（避免重复请求）
        max_comment_pages : int
            每个视频最多扫描多少页评论（每页 20 条）
        progress_callback : callable or None
            进度回调，接收 dict 参数

        Returns
        -------
        (comments, stats)
        """
        comments = []
        stats = {
            "video_scanned": 0,
            "total_videos": 0,
            "pages_scanned": 0,
            "comments_found": 0,
            "current_video": "",
            "phase": "准备中",
        }

        # 使用外部传入的用户信息
        if user_info:
            stats["user_name"] = user_info.get("name", str(uid))
        else:
            # 只有未传入时才主动获取
            if progress_callback:
                stats["phase"] = "正在获取用户信息"
                progress_callback(stats=stats)

            info = self.get_user_info(uid)
            if info is None:
                raise RuntimeError("获取用户信息失败，请检查 UID 是否正确")
            stats["user_name"] = info.get("name", str(uid))

            stat = self.get_user_stats(uid)
            user_info = {
                "name": info.get("name", str(uid)),
                "face": info.get("face", ""),
                "level": info.get("level", 0),
                "sign": info.get("sign", ""),
                "follower": stat.get("follower", 0) if stat else 0,
                "video_count": stat.get("video", 0) if stat else 0,
            }

        # --- 获取视频总数 ---
        if progress_callback:
            stats["phase"] = "正在获取视频列表"
            progress_callback(stats=stats)

        total = self.get_total_video_count(uid)
        stats["total_videos"] = min(total, depth * 50)

        # --- 逐页扫描视频 ---
        for page in range(1, depth + 1):
            if progress_callback:
                stats["phase"] = f"正在获取第 {page}/{depth} 页视频列表"
                progress_callback(stats=stats)

            vlist = self.get_user_videos(uid, page=page)
            if not vlist:
                break

            for v in vlist:
                stats["video_scanned"] += 1
                title = re.sub(r"<[^>]+>", "", v.get("title", ""))
                stats["current_video"] = title

                if progress_callback:
                    progress_callback(stats=stats)

                # 扫描该视频的评论
                v_comments = self._scan_single_video(
                    aid=v["aid"],
                    video_info=v,
                    target_uid=uid,
                    max_pages=max_comment_pages,
                    stats=stats,
                )
                comments.extend(v_comments)

                # 更新找到的评论数
                stats["comments_found"] = len(comments)

                # 视频间延迟
                time.sleep(random.uniform(1.0, 2.0))

        return comments, stats

    def _scan_single_video(self, aid, video_info, target_uid, max_pages, stats):
        """扫描单个视频的评论区，返回目标用户的评论列表"""
        found = []

        for cp in range(1, max_pages + 1):
            replies = self.get_video_comments(aid, page=cp)
            if not replies:
                break

            stats["pages_scanned"] += 1

            for reply in replies:
                if reply.get("mid") == target_uid:
                    # 清理评论中的 HTML 标签
                    msg = reply.get("content", {}).get("message", "")
                    msg = re.sub(r"<[^>]+>", "", msg)

                    pic = video_info.get("pic", "")
                    if pic and not pic.startswith("https"):
                        pic = "https:" + pic

                    found.append({
                        "video_title": re.sub(
                            r"<[^>]+>", "", video_info.get("title", "")
                        ),
                        "video_bvid": video_info.get("bvid", ""),
                        "video_pic": pic,
                        "aid": aid,
                        "comment": msg,
                        "time": reply.get("ctime", 0),
                        "likes": reply.get("like", 0),
                        "rpid": reply.get("rpid", 0),
                    })

            # 评论页间延迟
            time.sleep(random.uniform(0.5, 1.2))

        return found

    # ---------- 我的评论 ----------

    def get_my_comment_history(self, progress_callback=None):
        """通过消息中心获取当前登录用户的评论历史。

        调用 x/msgfeed/reply（回复通知），使用最大 ps=320 一次性拉取，
        提取用户原始评论并合并同一评论的多条回复。
        """
        self._ensure_session()

        stats = {
            "phase": "正在获取评论历史",
            "pages_scanned": 0,
            "total_pages": 1,
            "comments_found": 0,
        }

        if progress_callback:
            progress_callback(stats=stats)

        url = "https://api.bilibili.com/x/msgfeed/reply?ps=320"
        resp = self._request(url)
        if resp is None:
            raise RuntimeError("获取评论历史失败：请求超时或风控")

        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"获取评论历史失败: code={data.get('code')}")

        items = data.get("data", {}).get("items", [])
        stats["pages_scanned"] = 1

        # ---- 解析条目 ----
        raw = []
        for entry in items:
            item = entry.get("item", {})
            title = item.get("title", "") or ""
            root = item.get("root_reply_content", "") or ""
            target = item.get("target_reply_content", "") or ""
            source = item.get("source_content", "") or ""
            title = re.sub(r"<[^>]+>", "", title)
            root = re.sub(r"<[^>]+>", "", root)
            target = re.sub(r"<[^>]+>", "", target)
            source = re.sub(r"<[^>]+>", "", source)

            comment_text = title if title else root
            if not comment_text.strip():
                continue

            uri = item.get("uri", "")
            bvid = ""
            if "video/BV" in uri:
                m = re.search(r"video/(BV[\w]+)", uri)
                if m:
                    bvid = m.group(1)

            raw.append({
                "video_bvid": bvid,
                "video_pic": "",
                "aid": item.get("subject_id", 0),
                "comment": comment_text,
                "time": entry.get("reply_time", 0),
                "likes": 0,
                "rpid": item.get("root_id", 0),
                "reply_to_me": source,
                "target_content": target,
            })

        stats["comments_found"] = len(raw)
        if progress_callback:
            progress_callback(stats=stats)

        # ---- 合并去重：按 (comment + video_bvid) 分组 ----
        groups = {}
        for entry in raw:
            key = (entry["comment"], entry["video_bvid"])
            if key not in groups:
                groups[key] = dict(entry)
                groups[key]["replies"] = []
            if entry["reply_to_me"] and entry["reply_to_me"] not in groups[key]["replies"]:
                groups[key]["replies"].append(entry["reply_to_me"])
            # 取最早的时间
            if entry["time"] < groups[key]["time"]:
                groups[key]["time"] = entry["time"]

        comments = list(groups.values())
        for c in comments:
            c["reply_count"] = len(c["replies"])
            c.pop("reply_to_me", None)

        stats["comments_found"] = len(comments)
        return comments, stats
