"""
Netease Music Enhanced Plugin for AstrBot
- Author: NachoCrazy
- Repo: https://github.com/NachoCrazy/netease-music-astrbot-plugin
- Features: Interactive song selection, cover display, audio playback, and auto quality fallback.
"""

import re
import time
import base64
import aiohttp
import asyncio
import shutil
import urllib.parse
import os
import json
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List

from astrbot.api import star, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.message.message_event_result import MessageChain
from astrbot.api.message_components import Plain, Image, Record
try:
    from .qr_login import QRLogin
except Exception:
    try:
        from qr_login import QRLogin
    except Exception:
        QRLogin = None
        logger.warning("Netease Music plugin: qr_login module not importable; QR login disabled.")

try:
    from .text2image import Text2Image
except Exception:
    try:
        from text2image import Text2Image
    except Exception:
        Text2Image = None


# --- API Wrapper ---
class NeteaseMusicAPI:
    """
    A wrapper for the NeteaseCloudMusicApi to simplify interactions.
    Encapsulates API calls for searching, getting details, and fetching audio URLs.
    """
    def __init__(self, api_url: str, session: aiohttp.ClientSession):
        self.base_url = api_url.rstrip("/")
        self.session = session
        self.cookie: Optional[str] = None
        self.cookie_header: str = ""
        self.cookie_file = os.path.join(os.path.dirname(__file__), ".ncm_cookie.json")
        self._load_cookie()

    def _normalize_cookie(self, cookie: str) -> str:
        ignored_attrs = {"expires", "max-age", "path", "domain", "secure", "httponly", "samesite"}
        cookies: Dict[str, str] = {}
        for part in re.split(r";{1,2}", cookie or ""):
            part = part.strip()
            if not part or "=" not in part:
                continue
            name, value = part.split("=", 1)
            name = name.strip()
            if not name or name.lower() in ignored_attrs:
                continue
            cookies[name] = value.strip()
        return "; ".join(f"{name}={value}" for name, value in cookies.items())

    def _cookie_prefix(self, cookie: Optional[str] = None) -> str:
        value = cookie if cookie is not None else (self.cookie_header or self.cookie or "")
        return value[:15] if value else "none"

    def _log_cookie_operation(self, operation: str, request_id: str = "-", cookie: Optional[str] = None, detail: str = ""):
        logger.info(
            "Netease Music plugin: cookie_op "
            f"time={int(time.time() * 1000)}, op={operation}, request_id={request_id}, "
            f"cookie_prefix={self._cookie_prefix(cookie)}, detail={detail}"
        )

    def _cookie_names(self) -> str:
        if not self.cookie_header:
            return "none"
        return ",".join(item.split("=", 1)[0] for item in self.cookie_header.split("; ") if "=" in item)

    def _apply_cookie_to_session(self):
        session = self._active_session()
        if not session or not self.cookie_header:
            self._log_cookie_operation("apply_skip", detail=f"has_session={bool(session)}, has_cookie={bool(self.cookie_header)}")
            return
        try:
            self._log_cookie_operation("apply", cookie=self.cookie_header, detail=f"cookie_names={self._cookie_names()}")
            session.cookie_jar.update_cookies(dict(item.split("=", 1) for item in self.cookie_header.split("; ") if "=" in item))
            self._log_cookie_operation("apply_complete", cookie=self.cookie_header, detail=f"cookie_names={self._cookie_names()}")
        except Exception as e:
            logger.warning(f"Netease Music plugin: failed to update session cookie jar: {e!s}")

    def _load_cookie(self):
        try:
            if os.path.exists(self.cookie_file):
                with open(self.cookie_file, "r", encoding="utf-8") as fp:
                    data = json.load(fp)
                    self._log_cookie_operation("read", detail=f"path={self.cookie_file}")
                    self.cookie = data.get("cookie") or data.get("raw_cookie")
                    self.cookie_header = data.get("cookie_header") or self._normalize_cookie(self.cookie or "")
                self._log_cookie_operation("read_complete", cookie=self.cookie_header or self.cookie, detail=f"cookie_names={self._cookie_names()}")
                self._apply_cookie_to_session()
        except Exception as e:
            logger.warning(f"Netease Music plugin: failed to load saved cookie: {e!s}")

    def _save_cookie(self):
        if not self.cookie:
            self._log_cookie_operation("write_skip", detail="empty_cookie")
            return
        try:
            self._log_cookie_operation("write", cookie=self.cookie_header or self.cookie, detail=f"path={self.cookie_file}")
            with open(self.cookie_file, "w", encoding="utf-8") as fp:
                json.dump({"cookie": self.cookie, "cookie_header": self.cookie_header}, fp, ensure_ascii=False)
            os.chmod(self.cookie_file, 0o600)
            self._log_cookie_operation("write_complete", cookie=self.cookie_header or self.cookie, detail=f"path={self.cookie_file}")
        except Exception as e:
            logger.warning(f"Netease Music plugin: failed to save cookie: {e!s}")

    def set_cookie(self, cookie: str):
        self._log_cookie_operation("modify", cookie=cookie, detail="set_cookie")
        self.cookie = cookie.strip()
        self.cookie_header = self._normalize_cookie(self.cookie)
        self._log_cookie_operation("modify_complete", cookie=self.cookie_header or self.cookie, detail=f"cookie_names={self._cookie_names()}")
        self._apply_cookie_to_session()
        self._save_cookie()

    def clear_cookie(self):
        self._log_cookie_operation("delete", cookie=self.cookie_header or self.cookie, detail="clear_cookie")
        self.cookie = None
        self.cookie_header = ""
        session = self._active_session()
        if session:
            try:
                session.cookie_jar.clear()
                self._log_cookie_operation("delete_jar_cleared", detail="session cookie_jar cleared")
            except Exception as e:
                logger.warning(f"Netease Music plugin: failed to clear session cookie jar: {e!s}")
        try:
            if os.path.exists(self.cookie_file):
                os.remove(self.cookie_file)
                self._log_cookie_operation("delete_file_removed", detail=f"path={self.cookie_file}")
        except Exception as e:
            logger.warning(f"Netease Music plugin: failed to delete cookie file: {e!s}")

    def _active_session(self) -> Optional[aiohttp.ClientSession]:
        if self.session and not self.session.closed:
            return self.session
        return None

    async def _request(self, path: str, params: Optional[Dict[str, Any]] = None, no_cookie: bool = False) -> Dict[str, Any]:
        params = dict(params or {})
        params["timestamp"] = int(time.time() * 1000)
        headers = {}
        if self.cookie and not no_cookie:
            params["cookie"] = self.cookie
            if self.cookie_header:
                headers["Cookie"] = self.cookie_header
        url = f"{self.base_url}{path}"
        safe_params = {k: v for k, v in params.items() if k != "cookie"}
        self._log_cookie_operation("request_send", cookie=self.cookie_header or self.cookie, detail=f"path={path}, cookie_names={self._cookie_names()}")
        logger.info(f"Netease Music plugin: request path={path}, params={safe_params}, cookie_names={self._cookie_names()}")
        session = self._active_session()
        if session:
            async with session.get(url, params=params, headers=headers) as r:
                logger.info(f"Netease Music plugin: response path={path}, status={r.status}")
                r.raise_for_status()
                return await r.json()

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as temp:
            async with temp.get(url, params=params, headers=headers) as r:
                logger.info(f"Netease Music plugin: response path={path}, status={r.status}, temp_session=true")
                r.raise_for_status()
                return await r.json()

    async def search_songs(self, keyword: str, limit: int) -> List[Dict[str, Any]]:
        """Search for songs by keyword."""
        return (await self._request("/search", {"keywords": keyword, "limit": limit, "type": 1})).get("result", {}).get("songs", [])

    async def get_song_details(self, song_id: int) -> Optional[Dict[str, Any]]:
        """Get detailed information for a single song."""
        data = await self._request("/song/detail", {"ids": str(song_id)})
        songs = data.get("songs") or []
        logger.info(f"Netease Music plugin: song detail song_id={song_id}, code={data.get('code')}, songs_count={len(songs)}")
        return songs[0] if songs else None

    async def get_audio_url(self, song_id: int, quality: str) -> Optional[str]:
        """
        Get the audio stream URL for a song with automatic quality fallback.
        """
        qualities_to_try = list(dict.fromkeys([quality, "exhigh", "higher", "standard"]))
        for q in qualities_to_try:
            data = await self._request("/song/url/v1", {"id": str(song_id), "level": q})
            audio_info = data.get("data", [{}])[0]
            logger.info(
                "Netease Music plugin: audio url "
                f"song_id={song_id}, quality={q}, code={data.get('code')}, "
                f"url={'yes' if audio_info.get('url') else 'no'}, "
                f"freeTrialInfo={'yes' if audio_info.get('freeTrialInfo') else 'no'}, "
                f"fee={audio_info.get('fee')}, level={audio_info.get('level')}"
            )
            if audio_info.get("url"):
                return audio_info["url"]
        return None

    async def download_image(self, url: str) -> Optional[bytes]:
        """Download image data from a URL."""
        if not url:
            return None
        session = self._active_session()
        if session:
            async with session.get(url) as r:
                if r.status == 200:
                    return await r.read()
        else:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as temp:
                async with temp.get(url) as r:
                    if r.status == 200:
                        return await r.read()
        return None


# --- Main Plugin Class ---
class Main(star.Star):
    """
    A cat-maid themed Netease Music plugin that allows users to search for,
    select, and play songs directly in the chat.
    """
    def __init__(self, context, config: Optional[Dict[str, Any]] = None):
        super().__init__(context)
        self.config = config or {}

        # 从嵌套配置中提取扁平默认值
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
        self.song_cache: Dict[str, List[Dict[str, Any]]] = {}
        
        self.http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        self.api = NeteaseMusicAPI(self._cfg("api_url", "http://127.0.0.1:3000"), self.http_session)
        if QRLogin is not None:
            self.qr = QRLogin(self._cfg("api_url", "http://127.0.0.1:3000"), self.http_session)
        else:
            self.qr = None
        # text2image helper
        try:
            self.t2i = Text2Image(self)
        except Exception:
            self.t2i = None
        # 存储已登录的 cookie（仅内存）
        self.saved_logins: Dict[str, str] = {}
        
        self.cleanup_task: Optional[asyncio.Task] = None

    def _cfg(self, key: str, default: Any = None) -> Any:
        """Read config from nested ncm_api / short_url_api, fallback to default."""
        return self._cfg_defaults.get(key, default)

    # --- Lifecycle Hooks ---

    async def initialize(self):
        """Starts the background cleanup task when the plugin is activated."""
        self.cleanup_task = asyncio.create_task(self._periodic_cleanup())
        logger.info("Netease Music plugin: Background cleanup task started.")

    async def terminate(self):
        """Cleans up resources when the plugin is unloaded."""
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
        """A background task that runs periodically to clean up expired sessions."""
        while True:
            await asyncio.sleep(60)  # Run every 60 seconds
            now = time.time()
            expired_sessions = []
            
            for session_id, user_session in self.waiting_users.items():
                if user_session['expire'] < now:
                    expired_sessions.append((session_id, user_session['key']))
            
            if expired_sessions:
                logger.info(f"Netease Music plugin: Cleaning up {len(expired_sessions)} expired session(s).")
                for session_id, cache_key in expired_sessions:
                    if session_id in self.waiting_users:
                        del self.waiting_users[session_id]
                    if cache_key in self.song_cache:
                        del self.song_cache[cache_key]

    def _extract_command_arg(self, raw: str, commands: List[str]) -> Optional[str]:
        raw = raw.strip()
        for cmd in sorted(commands, key=len, reverse=True):
            match = re.match(rf"^#?{re.escape(cmd)}(?:\s+|$)(.*)$", raw, re.IGNORECASE)
            if match:
                return match.group(1).strip()
        return None

    # --- Event Handlers ---

    @filter.command("点歌", alias={"music", "听歌", "网易云"})
    async def cmd_handler(self, event: AstrMessageEvent, keyword: str = ""):
        """Handles the '/点歌' command."""
        # 从原始消息提取完整关键词（框架按空格分割会截断，只保留第一个词）
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

    @filter.command("ID点歌", alias={"点歌id", "songid", "musicid", "网易云id"})
    async def cmd_id_handler(self, event: AstrMessageEvent, song_id: str = ""):
        """Handles direct song playback by Netease song ID."""
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

    @filter.regex(r"(?i)^(来.?一首|播放|听.?听|点歌|唱.?一首|来.?首)\s*([^\s].+?)(的歌|的歌曲|的音乐|歌|曲)?$")
    async def natural_language_handler(self, event: AstrMessageEvent):
        """Handles song requests in natural language."""
        match = re.search(r"(?i)^(来.?一首|播放|听.?听|点歌|唱.?一首|来.?首)\s*([^\s].+?)(的歌|的歌曲|的音乐|歌|曲)?$", event.message_str)
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

    @filter.regex(r"^\d+(\s+(语音|链接|全部))?$", priority=999)
    async def number_selection_handler(self, event: AstrMessageEvent):
        """Handles user's numeric choice from the search results, optionally with send mode."""
        session_id = event.get_session_id()
        if session_id not in self.waiting_users:
            return

        user_session = self.waiting_users[session_id]
        if time.time() > user_session["expire"]:
            # Let the periodic cleanup handle the removal
            return

        text = event.message_str.strip()
        match = re.fullmatch(r"(\d+)(?:\s+(语音|链接|全部))?", text)
        if not match:
            return
        num = int(match.group(1))
        send_mode = match.group(2)  # None means use default

        limit = self._cfg("search_limit", 5)
        if not (1 <= num <= limit):
            return

        event.stop_event()

        # Fix: 先清掉 waiting_users，防止平台重试/重复事件导致重复发消息
        del self.waiting_users[session_id]

        await self.play_selected_song(event, user_session["key"], num, send_mode)

    # --- Core Logic ---

    async def search_and_show(self, event: AstrMessageEvent, keyword: str):
        """Searches for songs and displays the results to the user."""
        try:
            songs = await self.api.search_songs(keyword, self._cfg("search_limit", 5))
        except Exception as e:
            logger.error(f"Netease Music plugin: API search failed. Error: {e!s}")
            await event.send(MessageChain([Plain(f"-118 TimeOut 服务连接失败！")]))
            return

        if not songs:
            await event.send(MessageChain([Plain(f"404 {keyword} NotFound 未找到该歌曲")]))
            return

        cache_key = f"{event.get_session_id()}_{int(time.time())}"
        self.song_cache[cache_key] = songs

        response_lines = [f"为您找到了 {len(songs)} 首歌曲！请回复数字告诉我您想听哪一首（可附加 语音/链接/全部，如「1 链接」）"]
        for i, song in enumerate(songs, 1):
            artists = " / ".join(a["name"] for a in song.get("artists", []))
            album = song.get("album", {}).get("name", "未知专辑")
            duration_ms = song.get("duration", 0)
            dur_str = f"{duration_ms//60000}:{(duration_ms%60000)//1000:02d}"
            response_lines.append(f"{i}. {song['name']} - {artists} 《{album}》 [{dur_str}]")

        await event.send(MessageChain([Plain("\n".join(response_lines))]))

        self.waiting_users[event.get_session_id()] = {"key": cache_key, "expire": time.time() + self._cfg("wait_timeout", 60)}

    async def play_selected_song(self, event: AstrMessageEvent, cache_key: str, num: int, send_mode: Optional[str] = None):
        """Plays the song selected by the user."""
        if cache_key not in self.song_cache:
            await event.send(MessageChain([Plain("408 TimeOut 会话过期，请重新点歌")]))
            return

        songs = self.song_cache[cache_key]
        if not (1 <= num <= len(songs)):
             await event.send(MessageChain([Plain("400 CodeError 请输入正确的数字")]))
             return
             
        selected_song = songs[num - 1]
        song_id = selected_song["id"]
        
        try:
            song_details = await self.api.get_song_details(song_id)
            if not song_details:
                raise ValueError("400 InfoError")

            audio_url = await self.api.get_audio_url(song_id, self._cfg("quality", "exhigh"))
            if not audio_url:
                await event.send(MessageChain([Plain(f"400 NeedVIPError 该歌曲需要VIP权限才能播放")]))
                return

            title = song_details.get("name", "")
            artists = " / ".join(a["name"] for a in song_details.get("ar", []))
            album = song_details.get("al", {}).get("name", "未知专辑")
            cover_url = song_details.get("al", {}).get("picUrl", "")
            duration_ms = song_details.get("dt", 0)
            dur_str = f"{duration_ms//60000}:{(duration_ms%60000)//1000:02d}"

            await self._send_song_messages(event, num, song_id, title, artists, album, dur_str, cover_url, audio_url, send_mode)

        except Exception as e:
            logger.error(f"Netease Music plugin: Failed to play song {song_id}. Error: {e!s}")
            await event.send(MessageChain([Plain(f"400 InfoError 获取歌曲信息失败！")]))
        finally:
            if cache_key in self.song_cache:
                del self.song_cache[cache_key]

    async def play_song_by_id(self, event: AstrMessageEvent, song_id: int, send_mode: Optional[str] = None):
        """Plays a song directly by Netease song ID without searching."""
        logger.info(f"Netease Music plugin: play_song_by_id start song_id={song_id}, session_id={event.get_session_id()}")
        try:
            song_details = await self.api.get_song_details(song_id)
            if not song_details:
                logger.warning(f"Netease Music plugin: play_song_by_id detail not found song_id={song_id}")
                await event.send(MessageChain([Plain(f"404 {song_id} NotFound 未找到该歌曲")]))
                return

            audio_url = await self.api.get_audio_url(song_id, self._cfg("quality", "exhigh"))
            if not audio_url:
                logger.warning(f"Netease Music plugin: play_song_by_id audio url not found song_id={song_id}, quality={self._cfg('quality', 'exhigh')}")

                await event.send(MessageChain([Plain("400 NeedVIPError 该歌曲需要VIP权限才能播放")]))
                return

            title = song_details.get("name", "")
            artists = " / ".join(a["name"] for a in song_details.get("ar", []))
            album = song_details.get("al", {}).get("name", "未知专辑")
            cover_url = song_details.get("al", {}).get("picUrl", "")
            duration_ms = song_details.get("dt", 0)
            dur_str = f"{duration_ms//60000}:{(duration_ms%60000)//1000:02d}"
            logger.info(f"Netease Music plugin: play_song_by_id resolved song_id={song_id}, title={title!r}, artists={artists!r}")

            await self._send_song_messages(event, None, song_id, title, artists, album, dur_str, cover_url, audio_url, send_mode)

        except Exception as e:
            logger.error(f"Netease Music plugin: Failed to play song by id {song_id}. Error: {e!s}")
            await event.send(MessageChain([Plain("400 InfoError 获取歌曲信息失败！")]))

    def _resolve_send_mode(self, send_mode: Optional[str]) -> str:
        """Resolve send mode: user choice > per-request default > config default."""
        mode_map = {"语音": "voice", "voice": "voice", "链接": "url", "url": "url", "全部": "both", "both": "both"}
        if send_mode and send_mode in mode_map:
            return mode_map[send_mode]
        return self._cfg("audio_send_mode", "voice")

    async def _shorten_url(self, long_url: str) -> Optional[str]:
        """Shorten a URL via urlc.cn API. Returns shortened URL or None on failure."""
        if not self._cfg("short_url_enabled"):
            return None
        api_key = self._cfg("short_url_api_key")
        if not api_key:
            logger.warning("Netease Music plugin: short_url enabled but api_key is empty, skip shortening.")
            return None
        payload: Dict[str, Any] = {"url": long_url}
        domain = (self._cfg("short_url_domain") or "").strip()
        if domain:
            payload["domain"] = domain
        expiry_days = max(1, int(self._cfg("short_url_expiry_days", 1)))
        payload["expiry"] = (datetime.now() + timedelta(days=expiry_days)).strftime("%Y-%m-%d")
        logger.info(
            "Netease Music plugin: short_url request "
            f"url=https://www.urlc.cn/api/url/add, "
            f"api_key_prefix={api_key[:4]}..., "
            f"payload={json.dumps(payload, ensure_ascii=False)}"
        )
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
                async with session.post(
                    "https://www.urlc.cn/api/url/add",
                    json=payload,
                    headers={
                        "Authorization": f"Token {api_key}",
                        "Content-Type": "application/json",
                    },
                ) as r:
                    # API 可能返回 application/javascript 而非 application/json，手动解析
                    text = await r.text()
                    data = json.loads(text)
                    logger.info(
                        "Netease Music plugin: short_url api response "
                        f"status={r.status}, error={data.get('error')}, "
                        f"msg={data.get('msg', '')}"
                    )
                    if data.get("error") == 0 and data.get("short"):
                        short_url = data["short"]
                        logger.info(f"Netease Music plugin: short_url created: {short_url}")
                        return short_url
                    logger.warning(f"Netease Music plugin: short_url api failed or returned no url: {data}")
                    return None
        except Exception as e:
            logger.warning(f"Netease Music plugin: short_url request failed: {e!s}")
            return None

    async def _send_song_messages(self, event: AstrMessageEvent, num: Optional[int], song_id: int, title: str, artists: str, album: str, dur_str: str, cover_url: str, audio_url: str, send_mode: Optional[str] = None):
        """Constructs and sends the song info and audio messages."""
        resolved_mode = self._resolve_send_mode(send_mode)
        first_line = f"好，为您播放第 {num} 首歌曲" if num is not None else "好，为您播放指定 ID 歌曲"
        detail_text = f"""{first_line}

♪ 歌名：{title}
🎤 歌手：{artists}
💿 专辑：{album}
⏳ 时长：{dur_str}
🆔 ID：{song_id}
"""
        info_components = [Plain(detail_text)]

        if self._cfg("send_cover", True):
            image_data = await self.api.download_image(cover_url)
            if image_data:
                info_components.append(Image.fromBase64(base64.b64encode(image_data).decode()))

        await event.send(MessageChain(info_components))
        
        has_ffmpeg = shutil.which("ffmpeg") is not None
        send_voice = resolved_mode in ("voice", "both")
        send_link = resolved_mode in ("url", "both")

        if send_link:
            display_url = (await self._shorten_url(audio_url)) or audio_url
            await event.send(MessageChain([Plain(f"🔊 音频链接：{display_url}")]))

        if send_voice and has_ffmpeg:
            await event.send(MessageChain([Record(file=audio_url)]))
        elif send_voice and not has_ffmpeg:
            logger.warning("Netease Music plugin: ffmpeg 不可用，跳过发送语音。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("扫码登录", alias={"二维码登录", "qrlogin"})
    async def qr_login_handler(self, event: AstrMessageEvent):
        """生成二维码并轮询检测扫码状态，扫码成功后返回 cookie（若有）。"""
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
                    sent_qr_ref = await event.send(MessageChain([Plain("请使用网易云扫码登录，二维码 120s 内有效："), Image.fromBase64(qrimg_b64)]))
                except Exception as send_err:
                    logger.error(f"Netease Music plugin: Failed to send QR image, fallback to sending as file or link. Error: {send_err!s}")
                    # 尝试将 base64 保存为本地临时文件并以文件方式发送（部分平台对富媒体推送有限制，文件上传可能更稳健）
                    try:
                        import os
                        import base64 as _b64

                        data_b64 = qrimg_b64.split('base64,')[-1] if 'base64,' in qrimg_b64 else qrimg_b64
                        img_bytes = _b64.b64decode(data_b64)
                        cache_dir = os.path.join(os.getcwd(), '.qr_cache')
                        os.makedirs(cache_dir, exist_ok=True)
                        fname = os.path.join(cache_dir, f"{key}.png")
                        with open(fname, 'wb') as wf:
                            wf.write(img_bytes)

                        # 尝试使用 event.image_result 发送本地文件（适配器友好）
                        if hasattr(event, 'image_result'):
                            sent_qr_ref = await event.send(event.image_result(fname))
                        else:
                            # 退回到尝试使用 Image.fromFile，如果不可用则发送链接作为最后回退
                            try:
                                img_comp = Image.fromFile(fname)  # 可能不存在于某些版本
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
                    except Exception as e:
                        # logger.error(f"Netease Music plugin: QR check error: {e!s}")
                        await asyncio.sleep(2)
                        continue

                    code = res.get("code")
                    if code == 800:
                        await event.send(MessageChain([Plain("二维码已过期，请重新获取。")]))
                        return
                    if code == 801:
                        # 等待扫码
                        await asyncio.sleep(2)
                        continue
                    if code == 802:
                        await event.send(MessageChain([Plain("二维码已扫码，请在手机上确认登录。")]))
                        await asyncio.sleep(2)
                        continue
                    if code == 803:
                        # 登录成功，接口通常会返回 cookie（有的部署返回在 data 或 cookie 字段）
                        data = res.get("data") or {}
                        cookie_parts = []
                        if res.get("cookie"):
                            cookie_parts.append(res["cookie"])
                        if data.get("cookie"):
                            cookie_parts.append(data["cookie"])
                        cookie_parts.extend(res.get("set_cookies") or [])
                        cookie = ";;".join(cookie_parts)
                        profile = res.get("profile") or data.get("profile")

                        # 安全考虑：不要把 cookie 发送到聊天中。将 cookie 保存在内存中供后续使用。
                        try:
                            if cookie:
                                sid = event.get_session_id() if hasattr(event, 'get_session_id') else str(time.time())
                                self.saved_logins[sid] = cookie
                                try:
                                    self.api.set_cookie(cookie)
                                except Exception as save_err:
                                    logger.warning(f"Netease Music plugin: failed to set cookie for API: {save_err!s}")
                        except Exception:
                            # 不要因为保存失败而中断登录流程
                            logger.info("Netease Music plugin: failed to save cookie in memory; continuing.")

                        # 尝试撤回之前发送的二维码消息（若平台支持）并删除本地缓存文件
                        try:
                            if sent_qr_ref is not None:
                                try:
                                    if hasattr(event, 'recall'):
                                        await event.recall(sent_qr_ref)
                                    elif hasattr(event, 'delete'):
                                        await event.delete(sent_qr_ref)
                                    elif hasattr(event, 'delete_message'):
                                        await event.delete_message(sent_qr_ref)
                                except Exception:
                                    # 有些实现会返回一个包含 id 的映射/对象
                                    try:
                                        mid = None
                                        if isinstance(sent_qr_ref, dict):
                                            mid = sent_qr_ref.get('message_id') or sent_qr_ref.get('msg_id') or sent_qr_ref.get('id')
                                        elif hasattr(sent_qr_ref, 'message_id'):
                                            mid = getattr(sent_qr_ref, 'message_id')
                                        if mid and hasattr(event, 'delete_message'):
                                            await event.delete_message(mid)
                                    except Exception:
                                        logger.info("Netease Music plugin: retract QR message attempted but not supported or failed.")

                            # 删除本地缓存文件（若存在）
                            try:
                                import os
                                if qrimg_b64:
                                    keyfile = os.path.join(os.getcwd(), '.qr_cache', f"{key}.png")
                                    if os.path.exists(keyfile):
                                        os.remove(keyfile)
                            except Exception:
                                pass

                        except Exception:
                            logger.info("Netease Music plugin: failed to retract qr message or cleanup, ignored.")

                        # 最终给用户可见的成功提示（不包含 cookie）
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

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("退出登录", alias={"登出", "注销", "logout"})
    async def logout_handler(self, event: AstrMessageEvent):
        """清除所有 Cookie，退出登录。"""
        event.stop_event()
        self.api.clear_cookie()
        self.saved_logins.clear()
        logger.info("Netease Music plugin: admin logged out, cookie cleared.")
        await event.send(MessageChain([Plain("已退出登录，Cookie 信息已清除。")]))
