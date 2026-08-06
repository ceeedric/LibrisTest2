"""Talks to the stream API and turns its response into a playlist URL."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

from .config import ApiConfig
from .cookies import CookieStore

log = logging.getLogger(__name__)

_INDEX_PATTERN = re.compile(r"\[(-?\d+)\]")
_M3U8_PATTERN = re.compile(r"https?://[^\s\"'<>]+\.m3u8(?:\?[^\s\"'<>]*)?", re.IGNORECASE)

_MISSING = object()


@dataclass
class StreamStatus:
    """Outcome of a single poll for one username."""

    username: str
    online: bool
    url: str | None = None
    reason: str = ""


def dig(payload: Any, path: str) -> Any:
    """Resolve a dotted path like ``data.streams[0].url`` inside parsed JSON.

    Returns the ``_MISSING`` sentinel when any step of the path does not exist.
    """
    current = payload
    for segment in path.split("."):
        if not segment:
            continue
        name = _INDEX_PATTERN.sub("", segment)
        if name:
            if not isinstance(current, dict) or name not in current:
                return _MISSING
            current = current[name]
        for index in _INDEX_PATTERN.findall(segment):
            if not isinstance(current, list):
                return _MISSING
            try:
                current = current[int(index)]
            except IndexError:
                return _MISSING
    return current


def find_m3u8(payload: Any) -> str | None:
    """Depth-first scan of the payload for the first ``.m3u8`` URL."""
    if isinstance(payload, str):
        match = _M3U8_PATTERN.search(payload)
        return match.group(0) if match else None
    if isinstance(payload, dict):
        # Prefer obviously named keys before falling back to a full scan.
        preferred = ("hls", "hls_url", "hlsUrl", "playlist", "playlist_url", "stream_url", "url", "src")
        for key in preferred:
            if key in payload:
                found = find_m3u8(payload[key])
                if found:
                    return found
        for key, value in payload.items():
            if key in preferred:
                continue
            found = find_m3u8(value)
            if found:
                return found
        return None
    if isinstance(payload, list):
        for item in payload:
            found = find_m3u8(item)
            if found:
                return found
    return None


class StreamApi:
    """Thin wrapper around the per-username status endpoint."""

    def __init__(
        self,
        config: ApiConfig,
        client: httpx.AsyncClient,
        cookies: CookieStore | None = None,
    ) -> None:
        self._config = config
        self._client = client
        self._cookies = cookies

    async def check(self, username: str) -> StreamStatus:
        """Poll the API for ``username``.

        Raises :class:`httpx.HTTPError` on transport failures so the caller can
        back off; an offline stream is a normal result, not an exception.
        """
        url = self._config.url_for(username)
        response = await self._client.get(
            url,
            headers=self._config.headers or None,
            params=self._config.query or None,
            cookies=self._cookies.jar() if self._cookies else None,
        )

        if response.status_code in self._config.offline_status_codes:
            return StreamStatus(username, False, reason=f"HTTP {response.status_code}")
        response.raise_for_status()

        body = response.text.strip()
        if not body:
            return StreamStatus(username, False, reason="empty response body")

        try:
            payload: Any = response.json()
        except ValueError:
            # Some endpoints hand back the playlist URL (or the playlist) as plain text.
            payload = body

        return self._interpret(username, payload)

    def _interpret(self, username: str, payload: Any) -> StreamStatus:
        check = self._config.online_check
        if check.field_path:
            value = dig(payload, check.field_path)
            if value is _MISSING:
                return StreamStatus(username, False, reason=f"'{check.field_path}' missing from response")
            if value != check.equals:
                return StreamStatus(
                    username, False, reason=f"{check.field_path}={value!r} != {check.equals!r}"
                )

        stream_url: str | None = None
        if self._config.stream_url_field:
            value = dig(payload, self._config.stream_url_field)
            if value is _MISSING:
                log.debug("%s: field '%s' not present in response", username, self._config.stream_url_field)
            elif isinstance(value, str) and value.strip():
                stream_url = value.strip()
            elif value is not None:
                log.debug("%s: field '%s' is not a string (%r)", username, self._config.stream_url_field, value)

        if not stream_url and self._config.autodetect_m3u8:
            stream_url = find_m3u8(payload)

        if not stream_url:
            return StreamStatus(username, False, reason="no playlist URL in response")
        if not stream_url.startswith(("http://", "https://")):
            return StreamStatus(username, False, reason=f"playlist URL is not absolute: {stream_url!r}")

        return StreamStatus(username, True, url=stream_url, reason="live")


def build_client(config: ApiConfig, timeout: float) -> httpx.AsyncClient:
    """Create the shared HTTP client used for every poll."""
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout),
        follow_redirects=True,
        verify=config.verify_tls,
        headers={"Accept": "application/json, text/plain, */*"},
    )
