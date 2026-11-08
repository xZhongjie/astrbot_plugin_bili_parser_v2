"""B站视频解析 v2 —— 干净重写版。

设计原则：
* 配置项尽量少，没有历史兼容开关
* 视频只走第三方下载代理（kukutool/maxhelper），不碰 B 站 CDN
* 多线程 = 每个线程独立申请一个代理 token，避免单 token 被限流
"""

from __future__ import annotations

import asyncio
import base64
import json
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote
from typing import Any

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Plain, Video
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.star.filter.command import GreedyStr

PLUGIN_NAME = "astrbot_plugin_bili_parser_v2"
BILI_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
}
DEFAULT_PROXY_API = "https://dy.kukutool.com/api/download-proxy-url"
BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
SHORT_RE = re.compile(r"https?://b23\.tv/[0-9A-Za-z]+")
URL_RE = re.compile(r"https?://[^\s，,]+")

CARD_TEMPLATE = "【{title}】\nUP主：{up} · {duration}\n播放 {view} · 点赞 {like}\n{desc}\n{url}"


@dataclass
class Video:
    bvid: str = ""
    title: str = ""
    desc: str = ""
    cover: str = ""
    owner: str = ""
    duration: int = 0
    view: int = 0
    like: int = 0
    page: int = 1
    cid: int = 0
    url: str = ""
    play_url: str = ""


def fmt_count(value: Any) -> str:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return "0"
    if n >= 100000000:
        return f"{n / 100000000:.1f}亿"
    if n >= 10000:
        return f"{n / 10000:.1f}万"
    return str(n)


def fmt_duration(seconds: Any) -> str:
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return "-"
    return f"{s // 60}:{s % 60:02d}"


class BiliPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self.http: aiohttp.ClientSession | None = None
        self.data_dir: Path | None = None
        self._tasks: set[asyncio.Task] = set()

    async def initialize(self) -> None:
        self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        connector = aiohttp.TCPConnector(family=2)  # AF_INET
        self.http = aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=60),
            headers=BILI_HEADERS,
        )

    async def terminate(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self.http is not None:
            await self.http.close()

    # -- 解析 ---------------------------------------------------------------
    async def resolve_short(self, url: str) -> str:
        if self.http is None or "b23.tv" not in url:
            return url
        try:
            async with self.http.get(url, allow_redirects=True) as resp:
                return str(resp.url)
        except Exception as exc:
            logger.warning("[bili2] 短链解析失败：%s", exc)
            return url


    def miniapp_url(self, event: AstrMessageEvent) -> str:
        """从 QQ 小程序卡片里提取 B 站链接。"""
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        try:
            message = raw.get("message") if hasattr(raw, "get") else None
        except Exception:
            message = None
        if not isinstance(message, list):
            return ""
        for seg in message:
            if not isinstance(seg, dict):
                continue
            if seg.get("type") not in ("json", "light_app", "xml"):
                continue
            data = seg.get("data") or {}
            content = data.get("data") or data.get("content") or data.get("xml") or ""
            if not isinstance(content, str):
                continue
            for url in URL_RE.findall(content):
                if "bilibili.com" in url or "b23.tv" in url:
                    return url
        return ""

    async def extract(self, text: str) -> tuple[str, int]:
        match = BV_RE.search(text)
        if match:
            page = 1
            page_match = re.search(r"[?&]p=(\d+)", text)
            if page_match:
                page = max(1, int(page_match.group(1)))
            return match.group(1), page
        short = SHORT_RE.search(text)
        if short:
            resolved = await self.resolve_short(short.group(0))
            match = BV_RE.search(resolved)
            if match:
                return match.group(1), 1
        return "", 1

    async def fetch_info(self, bvid: str, page: int) -> Video | None:
        video = await self.fetch_xingzhige(bvid, page)
        if video is not None:
            return video
        return await self.fetch_official(bvid, page)

    async def fetch_xingzhige(self, bvid: str, page: int) -> Video | None:
        if self.http is None:
            return None
        target = quote(f"https://www.bilibili.com/video/{bvid}" + (f"?p={page}" if page > 1 else ""), safe="")
        url = f"https://api.xingzhige.com/API/b_parse/?url={target}"
        try:
            async with self.http.get(url) as resp:
                if resp.status != 200:
                    return None
                data = (await resp.json()).get("data") or {}
        except Exception as exc:
            logger.info("[bili2] 星之阁失败：%s", exc)
            return None
        if not data.get("bvid") or not data.get("title"):
            return None
        return Video(
            bvid=str(data.get("bvid") or bvid),
            title=str(data.get("title") or ""),
            desc=str(data.get("desc") or ""),
            cover=str(data.get("pic") or ""),
            owner=str((data.get("owner") or {}).get("name") or ""),
            duration=int(data.get("duration") or 0),
            view=int((data.get("stat") or {}).get("view") or 0),
            like=int((data.get("stat") or {}).get("like") or 0),
            page=page,
            cid=int(data.get("cid") or 0),
            url=f"https://www.bilibili.com/video/{bvid}" + (f"?p={page}" if page > 1 else ""),
            play_url=str((data.get("video") or {}).get("url") or ""),
        )

    async def fetch_official(self, bvid: str, page: int) -> Video | None:
        if self.http is None:
            return None
        payload = {}
        for attempt in range(2):
            buvid3 = "%032xinfoc" % random.getrandbits(128)
            try:
                async with self.http.get(
                    "https://api.bilibili.com/x/web-interface/view",
                    params={"bvid": bvid},
                    headers={**BILI_HEADERS, "Cookie": f"buvid3={buvid3}"},
                ) as resp:
                    if resp.status == 412:
                        continue
                    payload = await resp.json(content_type=None)
                    break
            except Exception as exc:
                logger.warning("[bili2] 官方接口失败：%s", exc)
        if not payload:
            return None
        data = payload.get("data") or {}
        if not data.get("bvid"):
            return None
        pages = data.get("pages") or [{}]
        index = min(max(page, 1), len(pages)) - 1
        return Video(
            bvid=str(data.get("bvid")),
            title=str(data.get("title") or ""),
            desc=str(data.get("desc") or ""),
            cover=str(data.get("pic") or ""),
            owner=str((data.get("owner") or {}).get("name") or ""),
            duration=int(data.get("duration") or 0),
            view=int((data.get("stat") or {}).get("view") or 0),
            like=int((data.get("stat") or {}).get("like") or 0),
            page=page,
            cid=int(data.get("cid") or 0),
            url=f"https://www.bilibili.com/video/{bvid}" + (f"?p={page}" if page > 1 else ""),
        )

    # -- 封面 ---------------------------------------------------------------
    async def cover_bytes(self, video: Video) -> bytes | None:
        if self.http is None or not video.cover:
            return None
        candidates = [video.cover, video.cover + "@480w_270h_1c.jpg"]
        for url in candidates:
            try:
                async with self.http.get(url, headers={"Range": "bytes=0-1048575"}) as resp:
                    data = await resp.read()
            except Exception:
                continue
            if data and len(data) > 1024 and (
                data[:2] == b"\xff\xd8" or data[:4] == b"\x89PNG" or data[:4] == b"RIFF"
            ):
                return data
            if data and b"ftyp" in data[:12]:
                continue
        return None

    # -- 代理视频下载 --------------------------------------------------------
    async def proxy_token(self) -> str:
        if self.http is None:
            return ""
        api = str(self.config.get("proxy_api") or DEFAULT_PROXY_API).strip()
        payload = {"url": "", "sourceUrl": "", "type": "video"}
        return api, payload  # type: ignore

    async def get_proxy_url(self, api: str, video: Video) -> str:
        if self.http is None:
            return ""
        payload = {
            "url": await self.play_url(video),
            "sourceUrl": video.url,
            "type": "video",
        }
        headers = {"Origin": "https://dy.kukutool.com", "Referer": "https://dy.kukutool.com/bilibili"}
        try:
            async with self.http.post(api, json=payload, headers=headers) as resp:
                if resp.status != 200:
                    return ""
                data = await resp.json()
                return str((data or {}).get("url") or "")
        except Exception as exc:
            logger.info("[bili2] 申请代理 token 失败：%s", exc)
            return ""

    async def total_size(self, url: str) -> tuple[int, str]:
        if self.http is None:
            return 0, ""
        try:
            async with self.http.get(url, headers={"Range": "bytes=0-0"}) as resp:
                range_header = resp.headers.get("Content-Range", "")
                total = int(range_header.rsplit("/", 1)[-1]) if "/" in range_header else 0
                return total, range_header
        except Exception:
            return 0, ""

    async def fetch_range(self, url: str, start: int, end: int) -> bytes:
        if self.http is None:
            return b""
        headers = {**BILI_HEADERS, "Range": f"bytes={start}-{end}"}
        try:
            async with self.http.get(url, headers=headers) as resp:
                if resp.status not in (200, 206):
                    return b""
                return await resp.read()
        except Exception:
            return b""

    async def play_url(self, video: Video) -> str:
        if video.play_url:
            return video.play_url
        if self.http is None or not video.cid:
            return ""
        for _ in range(2):
            buvid3 = "%032xinfoc" % random.getrandbits(128)
            try:
                async with self.http.get(
                    "https://api.bilibili.com/x/player/playurl",
                    params={"bvid": video.bvid, "cid": video.cid, "fnval": 1, "qn": 64},
                    headers={**BILI_HEADERS, "Cookie": f"buvid3={buvid3}"},
                ) as resp:
                    if resp.status == 412:
                        continue
                    data = await resp.json(content_type=None)
                durl = ((data.get("data") or {}).get("durl") or [])
                if durl:
                    return str(durl[0].get("url") or "")
            except Exception:
                continue
        return ""

    async def download_video(self, video: Video) -> bytes | None:
        api = str(self.config.get("proxy_api") or DEFAULT_PROXY_API).strip()
        if self.http is None or not api:
            return None
        threads = max(1, min(8, int(self.config.get("threads") or 4)))
        first = await self.get_proxy_url(api, video)
        if not first:
            logger.warning("[bili2] 代理接口未返回地址")
            return None
        total, _ = await self.total_size(first)
        if total <= 0:
            data = await self.fetch_range(first, 0, 10 * 1024 * 1024)
            return data if len(data) > 65536 else None

        chunk = max(256 * 1024, total // threads // 1024 // 256 * 256 * 1024)
        ranges = [(start, min(start + chunk - 1, total - 1)) for start in range(0, total, chunk)]
        sem = asyncio.Semaphore(threads)
        done = [0]
        lock = asyncio.Lock()
        started = time.monotonic()
        logger.info(
            "[bili2] 开始下载：%.1fMB，%d 线程，%d 个分片",
            total / 1024 / 1024, threads, len(ranges),
        )

        async def worker(start: int, end: int) -> bytes:
            want = end - start + 1
            for attempt in range(4):
                url = first if attempt == 0 else await self.get_proxy_url(api, video)
                if not url:
                    continue
                part = await self.fetch_range(url, start, end)
                if len(part) >= want:
                    async with lock:
                        done[0] += want
                        percent = done[0] / total * 100
                        elapsed = max(0.001, time.monotonic() - started)
                        logger.info(
                            "[bili2] 下载进度：%.0f%%（%.1f/%.1fMB，%.1fMB/s）",
                            percent, done[0] / 1024 / 1024,
                            total / 1024 / 1024, done[0] / elapsed / 1024 / 1024,
                        )
                    return part[:want]
            return b""

        async def guarded(start: int, end: int) -> bytes:
            async with sem:
                return await worker(start, end)

        results = await asyncio.gather(*(guarded(s, e) for s, e in ranges))
        if any(not part for part in results):
            logger.warning("[bili2] 有分片下载失败")
            return None
        data = b"".join(results)
        if len(data) != total:
            logger.warning("[bili2] 总长度不符：%d != %d", len(data), total)
            return None
        return data

    # -- 组装与发送 ----------------------------------------------------------
    def card_text(self, video: Video) -> str:
        desc = re.sub(r"\s+", " ", video.desc or "").strip()
        if len(desc) > 80:
            desc = desc[:80] + "…"
        template = str(self.config.get("template") or CARD_TEMPLATE)
        try:
            return template.format(
                title=video.title, up=video.owner or "未知",
                duration=fmt_duration(video.duration),
                view=fmt_count(video.view), like=fmt_count(video.like),
                desc=desc, url=video.url,
            )
        except Exception:
            return f"{video.title}\n{video.url}"

    async def send_card(self, event: AstrMessageEvent, video: Video) -> None:
        chain: list[Any] = []
        if bool(self.config.get("send_cover", True)):
            cover = await self.cover_bytes(video)
            if cover:
                chain.append(Image(file="base64://" + base64.b64encode(cover).decode()))
        chain.append(Plain(self.card_text(video)))
        try:
            await event.send(MessageChain(chain))
        except Exception as exc:
            logger.warning("[bili2] 卡片发送失败：%s", exc)
            await event.send(MessageChain([Plain(self.card_text(video))]))

    async def send_video_later(self, event: AstrMessageEvent, video: Video) -> None:
        data = await self.download_video(video)
        if not data:
            return
        if len(data) < 65536:
            return
        if self.data_dir is None:
            return
        path = self.data_dir / f"bili_{int(time.time())}_{random.randint(1000, 9999)}.mp4"
        await asyncio.to_thread(path.write_bytes, data)
        try:
            await event.send(MessageChain([Video(file=str(path))]))
        except Exception as exc:
            logger.warning("[bili2] 视频发送失败：%s", exc)
        finally:
            await asyncio.sleep(600)
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def schedule_video(self, event: AstrMessageEvent, video: Video) -> None:
        task = asyncio.create_task(self.send_video_later(event, video))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # -- 指令 ---------------------------------------------------------------

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def auto_parse(self, event: AstrMessageEvent):
        text = getattr(event, "message_str", "") or ""
        card_url = self.miniapp_url(event)
        if card_url:
            text = card_url
        if not ("bilibili.com" in text or "b23.tv" in text or BV_RE.search(text)):
            return
        bvid, page = await self.extract(text)
        if not bvid:
            return
        try:
            event.call_llm = True
        except Exception:
            pass
        video = await self.fetch_info(bvid, page)
        if video is None:
            return
        logger.info("[bili2] 解析成功：%s %s", video.bvid, video.title[:40])
        await self.send_card(event, video)
        logger.info("[bili2] 卡片已发送")
        if bool(self.config.get("send_video")) and video.play_url:
            self.schedule_video(event, video)    @filter.command("bili", alias={"b站解析", "哔哩哔哩解析"})
    async def bili(self, event: AstrMessageEvent, args: GreedyStr):
        text = str(args or "").strip()
        if not text:
            await event.send(MessageChain([Plain("用法：/bili BV号 或 视频链接")]))
            return
        bvid, page = await self.extract(text)
        if not bvid:
            if self.config.get("show_error"):
                await event.send(MessageChain([Plain("没有识别到 B 站链接或 BV 号")]))
            return
        video = await self.fetch_info(bvid, page)
        if video is None:
            if self.config.get("show_error"):
                await event.send(MessageChain([Plain("解析失败，请稍后再试")]))
            return
        logger.info("[bili2] 解析成功：%s %s", video.bvid, video.title[:40])
        await self.send_card(event, video)
        logger.info("[bili2] 卡片已发送")
        if bool(self.config.get("send_video")) and video.play_url:
            self.schedule_video(event, video)

