"""
Core song-playing logic for the Netease Music plugin.
Provides search, selection, playback, and URL shortening as a mixin class.
"""

import re
import time
import base64
import shutil
import aiohttp
import json
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.core.message.message_event_result import MessageChain
from astrbot.api.message_components import Plain, Image, Record


class SongCoreMixin:
    """Mixin providing core song search, selection, and playback logic."""

    # Type hints for attributes provided by Main class (duck typing)
    api: Any
    waiting_users: Dict[str, Dict[str, Any]]
    song_cache: Dict[str, List[Dict[str, Any]]]

    def _extract_command_arg(self, raw: str, commands: List[str]) -> Optional[str]:
        raw = raw.strip()
        for cmd in sorted(commands, key=len, reverse=True):
            match = re.match(rf"^#?{re.escape(cmd)}(?:\s+|$)(.*)$", raw, re.IGNORECASE)
            if match:
                return match.group(1).strip()
        return None

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

    async def _send_song_messages(
        self,
        event: AstrMessageEvent,
        num: Optional[int],
        song_id: int,
        title: str,
        artists: str,
        album: str,
        dur_str: str,
        cover_url: str,
        audio_url: str,
        send_mode: Optional[str] = None,
    ):
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

    async def search_and_show(self, event: AstrMessageEvent, keyword: str):
        """Searches for songs and displays the results to the user."""
        try:
            songs = await self.api.search_songs(keyword, self._cfg("search_limit", 5))
        except Exception as e:
            logger.error(f"Netease Music plugin: API search failed. Error: {e!s}")
            await event.send(MessageChain([Plain("-118 TimeOut 服务连接失败！")]))
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
            dur_str = f"{duration_ms // 60000}:{(duration_ms % 60000) // 1000:02d}"
            response_lines.append(f"{i}. {song['name']} - {artists} 《{album}》 [{dur_str}]")

        await event.send(MessageChain([Plain("\n".join(response_lines))]))

        self.waiting_users[event.get_session_id()] = {
            "key": cache_key,
            "expire": time.time() + self._cfg("wait_timeout", 60),
        }

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
                await event.send(MessageChain([Plain("400 NeedVIPError 该歌曲需要VIP权限才能播放")]))
                return

            title = song_details.get("name", "")
            artists = " / ".join(a["name"] for a in song_details.get("ar", []))
            album = song_details.get("al", {}).get("name", "未知专辑")
            cover_url = song_details.get("al", {}).get("picUrl", "")
            duration_ms = song_details.get("dt", 0)
            dur_str = f"{duration_ms // 60000}:{(duration_ms % 60000) // 1000:02d}"

            await self._send_song_messages(event, num, song_id, title, artists, album, dur_str, cover_url, audio_url, send_mode)

        except Exception as e:
            logger.error(f"Netease Music plugin: Failed to play song {song_id}. Error: {e!s}")
            await event.send(MessageChain([Plain("400 InfoError 获取歌曲信息失败！")]))
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
                logger.warning(
                    f"Netease Music plugin: play_song_by_id audio url not found "
                    f"song_id={song_id}, quality={self._cfg('quality', 'exhigh')}"
                )
                await event.send(MessageChain([Plain("400 NeedVIPError 该歌曲需要VIP权限才能播放")]))
                return

            title = song_details.get("name", "")
            artists = " / ".join(a["name"] for a in song_details.get("ar", []))
            album = song_details.get("al", {}).get("name", "未知专辑")
            cover_url = song_details.get("al", {}).get("picUrl", "")
            duration_ms = song_details.get("dt", 0)
            dur_str = f"{duration_ms // 60000}:{(duration_ms % 60000) // 1000:02d}"
            logger.info(f"Netease Music plugin: play_song_by_id resolved song_id={song_id}, title={title!r}, artists={artists!r}")

            await self._send_song_messages(event, None, song_id, title, artists, album, dur_str, cover_url, audio_url, send_mode)

        except Exception as e:
            logger.error(f"Netease Music plugin: Failed to play song by id {song_id}. Error: {e!s}")
            await event.send(MessageChain([Plain("400 InfoError 获取歌曲信息失败！")]))
