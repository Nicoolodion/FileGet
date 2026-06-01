from __future__ import annotations

import asyncio
import logging
from typing import Optional

import httpx

from .config import get_settings

log = logging.getLogger(__name__)

API_BASE = "https://www.mega-debrid.eu/api.php"


class DebridError(Exception):
    pass


class MegaDebridClient:
    def __init__(self) -> None:
        self._token: Optional[str] = None
        self._lock = asyncio.Lock()

    async def _connect(self, client: httpx.AsyncClient) -> str:
        s = get_settings()
        if not s.megadebrid_login or not s.megadebrid_password:
            raise DebridError("Mega-Debrid credentials are not configured (MEGADEBRID_LOGIN / MEGADEBRID_PASSWORD)")
        resp = await client.get(
            API_BASE,
            params={
                "action": "connectUser",
                "login": s.megadebrid_login,
                "password": s.megadebrid_password,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("response_code") != "ok":
            raise DebridError(f"Login failed: {data.get('response_text', 'unknown error')}")
        return data["token"]

    async def get_token(self, client: httpx.AsyncClient) -> str:
        async with self._lock:
            if not self._token:
                self._token = await self._connect(client)
            return self._token

    async def reset_token(self) -> None:
        async with self._lock:
            self._token = None

    async def get_debrid_link(self, link: str, password: str = "") -> str:
        async with httpx.AsyncClient(timeout=60) as client:
            token = await self.get_token(client)
            try:
                resp = await client.post(
                    API_BASE,
                    params={"action": "getLink", "token": token},
                    data={"link": link, "password": password},
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception:
                await self.reset_token()
                raise

            if data.get("response_code") != "ok":
                if "token" in str(data.get("response_text", "")).lower():
                    await self.reset_token()
                raise DebridError(f"Debrid failed: {data.get('response_text', 'unknown error')}")
            return data["debridLink"]


_client: Optional[MegaDebridClient] = None


def get_client() -> MegaDebridClient:
    global _client
    if _client is None:
        _client = MegaDebridClient()
    return _client
