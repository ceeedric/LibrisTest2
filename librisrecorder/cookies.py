"""Netscape ``cookies.txt`` support, shared by the API client and ffmpeg.

The file is re-read whenever its mtime changes, so a refreshed export takes
effect on the next poll without restarting the container.
"""

from __future__ import annotations

import http.cookiejar
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

_HTTP_ONLY_PREFIX = "#HttpOnly_"


@dataclass(frozen=True)
class Cookie:
    domain: str
    include_subdomains: bool
    path: str
    secure: bool
    expires: int  # unix seconds; 0 means a session cookie
    name: str
    value: str

    @property
    def expired(self) -> bool:
        return bool(self.expires) and self.expires < time.time()

    def as_pair(self) -> str:
        return f"{self.name}={self.value}"


def parse_netscape(text: str) -> list[Cookie]:
    """Parse Netscape/curl cookie-jar text, tolerating common export quirks."""
    cookies: list[Cookie] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        # yt-dlp and curl mark HttpOnly cookies with a pseudo-comment prefix;
        # the stdlib parser silently drops these, which is rarely what you want.
        if line.startswith(_HTTP_ONLY_PREFIX):
            line = line[len(_HTTP_ONLY_PREFIX):]
        elif line.startswith("#"):
            continue

        fields = line.split("\t")
        if len(fields) != 7:
            # Some editors turn tabs into spaces; keep the value intact.
            fields = line.split(None, 6)
        if len(fields) != 7:
            log.warning("cookies: ignoring malformed line %d (expected 7 fields)", lineno)
            continue

        domain, flag, path, secure, expires, name, value = fields
        try:
            expiry = int(float(expires))
        except ValueError:
            expiry = 0

        cookies.append(
            Cookie(
                domain=domain.strip(),
                include_subdomains=flag.strip().upper() == "TRUE" or domain.startswith("."),
                path=path.strip() or "/",
                secure=secure.strip().upper() == "TRUE",
                expires=expiry,
                name=name.strip(),
                value=value.strip(),
            )
        )
    return cookies


def _host_matches(cookie: Cookie, host: str) -> bool:
    domain = cookie.domain.lstrip(".").lower()
    host = host.lower()
    if not domain:
        return False
    if cookie.include_subdomains:
        return host == domain or host.endswith("." + domain)
    return host == domain


class CookieStore:
    """Loads cookies.txt on demand and hands them to httpx and ffmpeg."""

    def __init__(self, path: str | Path | None, send_to_api: bool = True, send_to_ffmpeg: bool = True) -> None:
        self.path = Path(path) if path else None
        self.send_to_api = send_to_api
        self.send_to_ffmpeg = send_to_ffmpeg
        self._cookies: list[Cookie] = []
        self._mtime: float | None = None
        self._jar: http.cookiejar.CookieJar | None = None

    @property
    def enabled(self) -> bool:
        return self.path is not None

    def load(self) -> list[Cookie]:
        """Return the current cookies, re-reading the file if it changed."""
        if self.path is None:
            return []
        try:
            mtime = self.path.stat().st_mtime
        except OSError as exc:
            if self._mtime is not None:
                log.warning("cookies: %s became unreadable (%s), using last known set", self.path, exc)
                return self._cookies
            log.error("cookies: cannot read %s: %s", self.path, exc)
            return []

        if mtime != self._mtime:
            try:
                text = self.path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                log.error("cookies: cannot read %s: %s", self.path, exc)
                return self._cookies
            self._cookies = parse_netscape(text)
            self._mtime = mtime
            self._jar = None

            expired = [c for c in self._cookies if c.expired]
            log.info(
                "cookies: loaded %d from %s%s",
                len(self._cookies), self.path,
                f" ({len(expired)} already expired)" if expired else "",
            )
            if expired and len(expired) == len(self._cookies):
                log.warning(
                    "cookies: every cookie in %s has expired — export a fresh file",
                    self.path,
                )
        return self._cookies

    def jar(self) -> http.cookiejar.CookieJar | None:
        """Cookie jar for httpx; it applies its own domain/path matching."""
        if not self.send_to_api:
            return None
        cookies = self.load()
        if not cookies:
            return None
        if self._jar is None:
            jar = http.cookiejar.CookieJar()
            for c in cookies:
                jar.set_cookie(
                    http.cookiejar.Cookie(
                        version=0,
                        name=c.name,
                        value=c.value,
                        port=None,
                        port_specified=False,
                        domain=c.domain,
                        domain_specified=True,
                        domain_initial_dot=c.domain.startswith("."),
                        path=c.path,
                        path_specified=True,
                        secure=c.secure,
                        expires=c.expires or None,
                        discard=False,
                        comment=None,
                        comment_url=None,
                        rest={},
                        rfc2109=False,
                    )
                )
            self._jar = jar
        return self._jar

    def cookie_header(self, url: str) -> str | None:
        """``Cookie:`` header value scoped to ``url``'s host, or None.

        Sent as a literal header rather than via ffmpeg's ``-cookies`` option:
        that option applies its own domain matching and was observed to drop
        cookies silently. We do the host filtering here instead, which also
        keeps credentials for other sites out of the ffmpeg argv when the
        playlist lives on a different CDN than the API.
        """
        if not self.send_to_ffmpeg:
            return None
        cookies = self.load()
        if not cookies:
            return None

        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        scheme = parts.scheme.lower()

        matching = [
            c for c in cookies
            if _host_matches(c, host) and not c.expired and not (c.secure and scheme != "https")
        ]
        if not matching:
            log.debug("cookies: none scoped to %s", host)
            return None
        log.debug("cookies: sending %d to %s", len(matching), host)
        return "; ".join(c.as_pair() for c in matching)
