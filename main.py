"""
Netease Music Enhanced Plugin for AstrBot
- Author: NachoCrazy
- Repo: https://github.com/NachoCrazy/netease-music-astrbot-plugin
- Features: Interactive song selection, cover display, audio playback, and auto quality fallback.
"""

import re
import time
import asyncio
import base64 as _base64
import os
import aiohttp
from typing import Dict, Any, Optional

from astrbot.api import star, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.message.message_event_result import MessageChain
from astrbot.api.message_components import Plain, Image

from .core.api import NeteaseMusicAPI
from .core.song_core import SongCoreMixin

try:
    from .core.qr_login import QRLogin
except Exception:
    try:
        from core.qr_login import QRLogin
    except Exception:
        QRLogin = None
        logger.warning("Netease Music plugin: qr_login module not importable; QR login disabled.")

try:
    from .core.text2image import Text2Image
except Exception:
    try:
        from core.text2image import Text2Image
    except Exception:
        Text2Image = None


class Main(star.Star, SongCoreMixin):
    """
    A cat-maid themed Netease Music plugin that allows users to search for,
    select, and play songs directly in the chat.
    """

    def __init__(self, context, config: Optional[Dict[str, Any]] = None):
        super().__init__(context)
        self.config = config or {}

        ncm = self.config.get("ncm_api", {}) or {}
        short = self.config.get("short_url_api", {}) or {}

        self._cfg_defaults: Dict[str, Any] = {
            # ncm_api
            "api_url": ncm.get("api_url", "http://127.0.0.1:3000"),
            "quality": ncm.get("quality", "exhigh"),
            "search_limit": ncm.get("search_limit", 5),
            "wait_timeout": ncm.get("wait_timeout", 60),
            "send_cover": ncm.get("send_cover", True),
            "audio_send_mode": ncm.get("audio_send_mode", "voice"),
            "enable_natural_language": ncm.get("enable_natural_language", True),
            # short_url_api
            "short_url_enabled": short.get("short_url_enabled", False),
            "short_url_api_key": short.get("short_url_api_key", ""),
            "short_url_domain": short.get("short_url_domain", ""),
            "short_url_expiry_days": short.get("short_url_expiry_days", 1),
        }

        self.waiting_users: Dict[str, Dict[str, Any]] = {}
        self.song_cache: Dict[str, list] = {}

        self.http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        self.api = NeteaseMusicAPI(self._cfg("api_url", "http://127.0.0.1:3000"), self.http_session, self.config)
        if QRLogin is not None:
            self.qr = QRLogin(self._cfg("api_url", "http://127.0.0.1:3000"), self.http_session)
        else:
            self.qr = None
        try:
            self.t2i = Text2Image(self)
        except Exception:
            self.t2i = None
        self.saved_logins: Dict[str, str] = {}

        self.cleanup_task: Optional[asyncio.Task] = None

    def _cfg(self, key: str, default: Any = None) -> Any:
        """从配置中读取值，嵌套 ncm_api / short_url_api 的扁平化访问。"""
        return self._cfg_defaults.get(key, default)

    # ==================== 生命周期 ====================

    async def initialize(self):
        self.cleanup_task = asyncio.create_task(self._periodic_cleanup())
        logger.info("Netease Music plugin: Background cleanup task started.")

    async def terminate(self):
        if self.cleanup_task and not self.cleanup_task.done():
            self.cleanup_task.cancel()
            try:
                await self.cleanup_task
            except asyncio.CancelledError:
                logger.info("Netease Music plugin: Background cleanup task cancelled.")

        if self.http_session and not self.http_session.closed:
            await self.http_session.close()
            logger.info("Netease Music plugin: HTTP session closed.")

    async def _periodic_cleanup(self):
        while True:
            await asyncio.sleep(60)
            now = time.time()
            expired_sessions = []
            for session_id, user_session in self.waiting_users.items():
                if user_session["expire"] < now:
                    expired_sessions.append((session_id, user_session["key"]))
            if expired_sessions:
                logger.info(f"Netease Music plugin: Cleaning up {len(expired_sessions)} expired session(s).")
                for session_id, cache_key in expired_sessions:
                    if session_id in self.waiting_users:
                        del self.waiting_users[session_id]
                    if cache_key in self.song_cache:
                        del self.song_cache[cache_key]

    # ==================== 命令处理器：点歌 ====================

    @filter.command("点歌", alias={"music", "听歌", "网易云"})
    async def cmd_handler(self, event: AstrMessageEvent, keyword: str = ""):
        raw = event.message_str.strip()
        parsed_keyword = self._extract_command_arg(raw, ["点歌", "music", "听歌", "网易云"])
        if parsed_keyword is None:
            return
        keyword = parsed_keyword or keyword.strip()
        logger.info(f"Netease Music plugin: cmd_handler raw={raw!r}, keyword={keyword!r}")
        if not keyword:
            await event.send(MessageChain([Plain("请告诉我您想听什么歌 例如：#点歌 Lemon 或 #ID点歌 33894312")]))
            event.stop_event()
            return
        event.stop_event()
        id_match = re.fullmatch(r"(?:id\s*)?(\d{4,})(?:\s+(语音|链接|全部))?$", keyword, re.IGNORECASE)
        if id_match:
            await self.play_song_by_id(event, int(id_match.group(1)), send_mode=id_match.group(2))
            return
        await self.search_and_show(event, keyword)

    # ==================== 命令处理器：ID 点歌 ====================

    @filter.command("ID点歌", alias={"点歌id", "songid", "musicid", "网易云id"})
    async def cmd_id_handler(self, event: AstrMessageEvent, song_id: str = ""):
        raw = event.message_str.strip()
        parsed_song_id = self._extract_command_arg(raw, ["ID点歌", "id点歌", "点歌id", "songid", "musicid", "网易云id"])
        if parsed_song_id is None:
            return
        song_id = parsed_song_id or song_id.strip()
        logger.info(f"Netease Music plugin: cmd_id_handler raw={raw!r}, song_id_arg={song_id!r}")
        id_mode_match = re.fullmatch(r"(\d+)(?:\s+(语音|链接|全部))?$", song_id or "")
        if not id_mode_match:
            await event.send(MessageChain([Plain("请提供正确的歌曲 ID 例如：#ID点歌 33894312 或 #ID点歌 33894312 链接")]))
            event.stop_event()
            return
        event.stop_event()
        await self.play_song_by_id(event, int(id_mode_match.group(1)), send_mode=id_mode_match.group(2))

    # ==================== 事件处理器：自然语言点歌 ====================

    @filter.regex(r"(?i)^(来.?一首|播放|听.?听|点歌|唱.?一首|来.?首)\s*([^\s].+?)(的歌|的歌曲|的音乐|歌|曲)?$")
    async def natural_language_handler(self, event: AstrMessageEvent):
        match = re.search(
            r"(?i)^(来.?一首|播放|听.?听|点歌|唱.?一首|来.?首)\s*([^\s].+?)(的歌|的歌曲|的音乐|歌|曲)?$",
            event.message_str,
        )
        if match:
            keyword = match.group(2).strip()
            if keyword:
                logger.info(f"Netease Music plugin: natural_language_handler raw={event.message_str!r}, keyword={keyword!r}")
                event.stop_event()
                id_match = re.fullmatch(r"(?:id\s*)?(\d{4,})(?:\s+(语音|链接|全部))?$", keyword, re.IGNORECASE)
                if id_match:
                    await self.play_song_by_id(event, int(id_match.group(1)), send_mode=id_match.group(2))
                    return
                await self.search_and_show(event, keyword)

    # ==================== 事件处理器：数字选歌 ====================

    @filter.regex(r"^\d+(\s+(语音|链接|全部))?$", priority=999)
    async def number_selection_handler(self, event: AstrMessageEvent):
        session_id = event.get_session_id()
        if session_id not in self.waiting_users:
            return

        user_session = self.waiting_users[session_id]
        if time.time() > user_session["expire"]:
            return

        text = event.message_str.strip()
        match = re.fullmatch(r"(\d+)(?:\s+(语音|链接|全部))?", text)
        if not match:
            return
        num = int(match.group(1))
        send_mode = match.group(2)

        limit = self._cfg("search_limit", 5)
        if not (1 <= num <= limit):
            return

        event.stop_event()
        del self.waiting_users[session_id]
        await self.play_selected_song(event, user_session["key"], num, send_mode)

    # ==================== 管理员命令：扫码登录 ====================

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("扫码登录", alias={"二维码登录", "qrlogin"})
    async def qr_login_handler(self, event: AstrMessageEvent):
        event.stop_event()
        if not self.qr:
            await event.send(MessageChain([Plain("二维码登录模块不可用，请检查插件文件 qr_login.py 是否存在。")]))
            return
        try:
            key = await self.qr.create_key()
            if not key:
                await event.send(MessageChain([Plain("生成二维码失败，请稍后重试。")]))
                return

            qr = await self.qr.create_qr(key)
            qrimg_b64 = qr.get("qrimg")
            qrurl = qr.get("qrurl")

            sent_qr_ref = None
            if qrimg_b64:
                try:
                    sent_qr_ref = await event.send(
                        MessageChain([Plain("请使用网易云扫码登录，二维码 120s 内有效："), Image.fromBase64(qrimg_b64)])
                    )
                except Exception as send_err:
                    logger.error(f"Netease Music plugin: Failed to send QR image, fallback to sending as file or link. Error: {send_err!s}")
                    try:
                        data_b64 = qrimg_b64.split("base64,")[-1] if "base64," in qrimg_b64 else qrimg_b64
                        img_bytes = _base64.b64decode(data_b64)
                        cache_dir = os.path.join(os.getcwd(), ".qr_cache")
                        os.makedirs(cache_dir, exist_ok=True)
                        fname = os.path.join(cache_dir, f"{key}.png")
                        with open(fname, "wb") as wf:
                            wf.write(img_bytes)

                        if hasattr(event, "image_result"):
                            sent_qr_ref = await event.send(event.image_result(fname))
                        else:
                            try:
                                img_comp = Image.fromFile(fname)
                                sent_qr_ref = await event.send(MessageChain([img_comp]))
                            except Exception:
                                if qrurl:
                                    sent_qr_ref = await event.send(MessageChain([Plain(f"无法发送图片附件，改为使用以下链接扫码登录：{qrurl}")]))
                                else:
                                    sent_qr_ref = await event.send(MessageChain([Plain("无法发送二维码图片及链接，请稍后重试。")]))
                    except Exception as save_err:
                        logger.error(f"Netease Music plugin: Failed to save/send QR image file fallback: {save_err!s}")
                        if qrurl:
                            await event.send(MessageChain([Plain(f"无法发送图片，改为使用以下链接扫码登录：{qrurl}")]))
                        else:
                            await event.send(MessageChain([Plain("无法发送二维码图片且未返回扫码链接，请稍后重试或检查服务部署。")]))
            else:
                sent_qr_ref = await event.send(MessageChain([Plain(f"请使用以下链接扫码登录：{qrurl}")]))

            async def _poll():
                expire = time.time() + 120
                while time.time() < expire:
                    try:
                        res = await self.qr.check(key)
                    except Exception:
                        await asyncio.sleep(2)
                        continue

                    code = res.get("code")
                    if code == 800:
                        await event.send(MessageChain([Plain("二维码已过期，请重新获取。")]))
                        return
                    if code == 801:
                        await asyncio.sleep(2)
                        continue
                    if code == 802:
                        await event.send(MessageChain([Plain("二维码已扫码，请在手机上确认登录。")]))
                        await asyncio.sleep(2)
                        continue
                    if code == 803:
                        data = res.get("data") or {}
                        cookie_parts = []
                        if res.get("cookie"):
                            cookie_parts.append(res["cookie"])
                        if data.get("cookie"):
                            cookie_parts.append(data["cookie"])
                        cookie_parts.extend(res.get("set_cookies") or [])
                        cookie = ";;".join(cookie_parts)
                        profile = res.get("profile") or data.get("profile")

                        try:
                            if cookie:
                                sid = event.get_session_id() if hasattr(event, "get_session_id") else str(time.time())
                                self.saved_logins[sid] = cookie
                                try:
                                    self.api.set_cookie(cookie)
                                except Exception as save_err:
                                    logger.warning(f"Netease Music plugin: failed to set cookie for API: {save_err!s}")
                        except Exception:
                            logger.info("Netease Music plugin: failed to save cookie in memory; continuing.")

                        try:
                            if sent_qr_ref is not None:
                                try:
                                    if hasattr(event, "recall"):
                                        await event.recall(sent_qr_ref)
                                    elif hasattr(event, "delete"):
                                        await event.delete(sent_qr_ref)
                                    elif hasattr(event, "delete_message"):
                                        await event.delete_message(sent_qr_ref)
                                except Exception:
                                    try:
                                        mid = None
                                        if isinstance(sent_qr_ref, dict):
                                            mid = sent_qr_ref.get("message_id") or sent_qr_ref.get("msg_id") or sent_qr_ref.get("id")
                                        elif hasattr(sent_qr_ref, "message_id"):
                                            mid = getattr(sent_qr_ref, "message_id")
                                        if mid and hasattr(event, "delete_message"):
                                            await event.delete_message(mid)
                                    except Exception:
                                        logger.info("Netease Music plugin: retract QR message attempted but not supported or failed.")

                            try:
                                if qrimg_b64:
                                    keyfile = os.path.join(os.getcwd(), ".qr_cache", f"{key}.png")
                                    if os.path.exists(keyfile):
                                        os.remove(keyfile)
                            except Exception:
                                pass

                        except Exception:
                            logger.info("Netease Music plugin: failed to retract qr message or cleanup, ignored.")

                        await event.send(MessageChain([Plain("扫码登录成功！")]))
                        if profile:
                            try:
                                uname = profile.get("nickname") or profile.get("nickname", "未知用户")
                                await event.send(MessageChain([Plain(f"登录用户：{uname}")]))
                            except Exception:
                                pass

                        return

                    await asyncio.sleep(2)

                await event.send(MessageChain([Plain("二维码登录超时，请重试。")]))

            asyncio.create_task(_poll())

        except Exception as e:
            logger.error(f"Netease Music plugin: QR login failed: {e!s}")
            await event.send(MessageChain([Plain("二维码登录失败，请稍后重试。")]))

    # ==================== 管理员命令：退出登录 ====================

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("退出登录", alias={"登出", "注销", "logout"})
    async def logout_handler(self, event: AstrMessageEvent):
        event.stop_event()
        self.api.clear_cookie()
        self.saved_logins.clear()
        logger.info("Netease Music plugin: admin logged out, cookie cleared.")
        await event.send(MessageChain([Plain("已退出登录，Cookie 信息已清除。")]))
