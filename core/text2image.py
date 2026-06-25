import hashlib
import time
import asyncio
import aiohttp
from typing import Any, Dict, Optional


class Text2Image:
    """A small wrapper around AstrBot's html/text->image APIs.

    Features:
    - retries on transient failures
    - simple in-memory caching by content hash
    - optional local download of rendered image
    """

    def __init__(self, star_instance, http_session: aiohttp.ClientSession = None):
        self.star = star_instance
        self.session = http_session
        self._cache: Dict[str, str] = {}

    def _hash(self, template: str, data: Optional[Dict[str, Any]], options: Optional[Dict[str, Any]]):
        h = hashlib.sha1()
        h.update(template.encode('utf-8'))
        h.update(repr(data).encode('utf-8'))
        h.update(repr(options).encode('utf-8'))
        return h.hexdigest()

    async def render_html(self, template: str, data: Optional[Dict[str, Any]] = None, options: Optional[Dict[str, Any]] = None, *, retries: int = 2, save_local: bool = False, timeout: int = 15) -> Optional[str]:
        key = self._hash(template, data, options)
        if key in self._cache:
            return self._cache[key]

        last_err = None
        for attempt in range(retries + 1):
            try:
                # Prefer html_render if available
                if hasattr(self.star, 'html_render'):
                    coro = self.star.html_render(template, data or {}, options=options or {})
                else:
                    # Fallback: render template by filling and pass to text_to_image
                    filled = template
                    if data:
                        try:
                            filled = template.format(**data)
                        except Exception:
                            pass
                    coro = self.star.text_to_image(filled)

                # wait with timeout
                url = await asyncio.wait_for(coro, timeout=timeout)

                if save_local:
                    # download to local file
                    if not self.session:
                        self.session = aiohttp.ClientSession()
                    async with self.session.get(url) as r:
                        r.raise_for_status()
                        b = await r.read()
                    fname = f"./.t2i_cache/{key}.png"
                    try:
                        import os
                        os.makedirs(os.path.dirname(fname), exist_ok=True)
                        with open(fname, 'wb') as f:
                            f.write(b)
                        final = fname
                    except Exception:
                        final = url
                else:
                    final = url

                # cache and return
                self._cache[key] = final
                return final

            except Exception as e:
                last_err = e
                await asyncio.sleep(1 + attempt)
                continue

        # all retries failed
        raise last_err
