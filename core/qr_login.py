import time
import urllib.parse
import aiohttp
from typing import Optional, Dict, Any


class QRLogin:
    """独立模块：处理网易云 API 的二维码登录相关接口。

    提供：生成 key、生成二维码（base64）、轮询检测扫码状态。
    """

    def __init__(self, base_url: str, session):
        self.base_url = base_url.rstrip("/")
        self.session = session

    def _active_session(self) -> Optional[aiohttp.ClientSession]:
        if self.session and not getattr(self.session, "closed", True):
            return self.session
        return None

    async def _get(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        params = dict(params)
        params["timestamp"] = int(time.time() * 1000)
        url = f"{self.base_url}{path}"
        session = self._active_session()
        if session:
            async with session.get(url, params=params) as r:
                r.raise_for_status()
                data = await r.json()
                cookies = r.headers.getall("Set-Cookie", []) if hasattr(r.headers, "getall") else []
                if cookies:
                    data["set_cookies"] = cookies
                return data

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as temp:
            async with temp.get(url, params=params) as r:
                r.raise_for_status()
                data = await r.json()
                cookies = r.headers.getall("Set-Cookie", []) if hasattr(r.headers, "getall") else []
                if cookies:
                    data["set_cookies"] = cookies
                return data

    async def create_key(self) -> Optional[str]:
        data = await self._get("/login/qr/key", {})
        return data.get("data", {}).get("unikey")

    async def create_qr(self, key: str, qrimg: bool = True) -> Dict[str, Optional[str]]:
        params = {
            "key": key,
            "qrimg": str(qrimg).lower(),
        }
        data = await self._get("/login/qr/create", params)
        d = data.get("data") or {}
        return {"qrimg": d.get("qrimg"), "qrurl": d.get("qrurl")}

    async def check(self, key: str) -> Dict[str, Any]:
        return await self._get("/login/qr/check", {"key": key})
