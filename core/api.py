"""
NeteaseCloudMusicApi wrapper for the Netease Music plugin.
Encapsulates API calls for searching, getting details, fetching audio URLs,
and managing session cookies via AstrBot plugin config.
"""

import re
import time
import json
import os
import aiohttp
from typing import Dict, Any, Optional, List

from astrbot.api import logger


class NeteaseMusicAPI:
    """
    A wrapper for the NeteaseCloudMusicApi to simplify interactions.
    Encapsulates API calls for searching, getting details, and fetching audio URLs.
    Cookie is stored in the AstrBot plugin config (ncm_api.cookie).
    """

    _OLD_COOKIE_FILE = os.path.join(os.path.dirname(__file__), "..", ".ncm_cookie.json")

    def __init__(self, api_url: str, session: aiohttp.ClientSession, config: Optional[Dict[str, Any]] = None):
        self.base_url = api_url.rstrip("/")
        self.session = session
        self._config = config  # AstrBotConfig (dict-like, supports save_config())
        self.cookie: Optional[str] = None
        self.cookie_header: str = ""
        self._load_cookie()

    # ---- cookie normalisation / logging helpers (unchanged) ----

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

    # ---- cookie storage via AstrBot config ----

    def _read_cookie_from_config(self) -> str:
        """Read raw cookie string from AstrBot plugin config."""
        if not self._config:
            return ""
        ncm = self._config.get("ncm_api", {})
        if isinstance(ncm, dict):
            return ncm.get("cookie", "")
        return ""

    def _write_cookie_to_config(self):
        """Persist the current cookie into AstrBot plugin config and save."""
        if self._config is None:
            self._log_cookie_operation("write_skip", detail="no_config")
            return
        self._log_cookie_operation("write", cookie=self.cookie_header or self.cookie, detail="into plugin config")
        ncm = self._config.setdefault("ncm_api", {})
        ncm["cookie"] = self.cookie or ""
        try:
            if hasattr(self._config, "save_config"):
                self._config.save_config()
                self._log_cookie_operation("write_complete", cookie=self.cookie_header or self.cookie)
        except Exception as e:
            logger.warning(f"Netease Music plugin: failed to save config: {e!s}")

    def _load_cookie(self):
        """Load cookie from AstrBot plugin config (with legacy file migration)."""
        raw = self._read_cookie_from_config()
        # Migrate from legacy .ncm_cookie.json if config is empty
        if not raw and os.path.exists(self._OLD_COOKIE_FILE):
            try:
                with open(self._OLD_COOKIE_FILE, "r", encoding="utf-8") as fp:
                    data = json.load(fp)
                    raw = data.get("cookie") or data.get("raw_cookie") or ""
                    self._log_cookie_operation("read_legacy", detail=f"path={self._OLD_COOKIE_FILE}")
                if raw:
                    self.cookie = raw.strip()
                    self.cookie_header = self._normalize_cookie(self.cookie)
                    self._write_cookie_to_config()
                    os.remove(self._OLD_COOKIE_FILE)
                    self._log_cookie_operation("migrated", cookie=self.cookie_header or self.cookie)
                    self._apply_cookie_to_session()
                    return
            except Exception as e:
                logger.warning(f"Netease Music plugin: failed to migrate legacy cookie: {e!s}")

        if raw:
            self._log_cookie_operation("read", detail="from plugin config")
            self.cookie = raw.strip()
            self.cookie_header = self._normalize_cookie(self.cookie)
            self._log_cookie_operation("read_complete", cookie=self.cookie_header or self.cookie, detail=f"cookie_names={self._cookie_names()}")
            self._apply_cookie_to_session()

    def set_cookie(self, cookie: str):
        self._log_cookie_operation("modify", cookie=cookie, detail="set_cookie")
        self.cookie = cookie.strip()
        self.cookie_header = self._normalize_cookie(self.cookie)
        self._log_cookie_operation("modify_complete", cookie=self.cookie_header or self.cookie, detail=f"cookie_names={self._cookie_names()}")
        self._apply_cookie_to_session()
        self._write_cookie_to_config()

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
        if self._config is not None:
            ncm = self._config.setdefault("ncm_api", {})
            ncm["cookie"] = ""
            try:
                if hasattr(self._config, "save_config"):
                    self._config.save_config()
                    self._log_cookie_operation("delete_config_cleared")
            except Exception as e:
                logger.warning(f"Netease Music plugin: failed to save config after clearing cookie: {e!s}")

    # ---- HTTP session helpers ----

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

        async def _do_get(sess: aiohttp.ClientSession):
            async with sess.get(url, params=params, headers=headers) as r:
                logger.info(f"Netease Music plugin: response path={path}, status={r.status}")
                r.raise_for_status()
                return await r.json()

        session = self._active_session()
        try:
            if session:
                return await _do_get(session)
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as temp:
                return await _do_get(temp)
        except aiohttp.ClientError as e:
            logger.error(f"Netease Music plugin: request failed path={path}, error={e!s}")
            return {}

    # ---- public API methods ----

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
