"""Per-username poll → record → cooldown loop."""

from __future__ import annotations

import asyncio
import logging
import random

import httpx

from .api import StreamApi
from .config import Config
from .recorder import DiskSpaceError, FfmpegRecorder

log = logging.getLogger(__name__)


class UserWorker:
    """Owns one username: polls it, and records whenever it goes live."""

    def __init__(
        self,
        username: str,
        config: Config,
        api: StreamApi,
        slots: asyncio.Semaphore | None,
        shutdown: asyncio.Event,
    ) -> None:
        self.username = username
        self._config = config
        self._api = api
        self._slots = slots
        self._shutdown = shutdown
        self._recorder: FfmpegRecorder | None = None
        self._backoff = config.poll.retry_backoff_seconds

    async def run(self) -> None:
        poll = self._config.poll
        if poll.stagger_start and poll.interval_seconds > 0:
            # Spread the first poll of each user across the interval.
            await self._sleep(random.uniform(0, min(poll.interval_seconds, 30.0)))

        while not self._shutdown.is_set():
            try:
                status = await self._api.check(self.username)
            except httpx.HTTPError as exc:
                log.warning("[%s] poll failed: %s — retrying in %.0fs", self.username, exc, self._backoff)
                await self._sleep(self._backoff)
                self._backoff = min(self._backoff * 2, poll.max_backoff_seconds)
                continue
            except Exception:
                log.exception("[%s] unexpected error while polling", self.username)
                await self._sleep(self._backoff)
                self._backoff = min(self._backoff * 2, poll.max_backoff_seconds)
                continue

            self._backoff = poll.retry_backoff_seconds

            if not status.online or not status.url:
                log.debug("[%s] offline (%s)", self.username, status.reason)
                await self._sleep(self._next_interval())
                continue

            log.info("[%s] live — starting recording", self.username)
            await self._record(status.url)

            if self._shutdown.is_set():
                break
            await self._sleep(self._config.recording.cooldown_seconds)

        log.debug("[%s] worker stopped", self.username)

    async def _record(self, url: str) -> None:
        if self._slots is not None:
            if self._slots.locked():
                log.info("[%s] all recording slots busy, waiting", self.username)
            await self._slots.acquire()
        try:
            self._recorder = FfmpegRecorder(self._config.recording, self.username)
            await self._recorder.run(url)
        except DiskSpaceError as exc:
            log.error("[%s] skipping recording: %s", self.username, exc)
            await self._sleep(self._config.poll.interval_seconds)
        except FileNotFoundError:
            log.error(
                "[%s] ffmpeg not found at %r — is it installed in the image?",
                self.username, self._config.recording.ffmpeg_path,
            )
            await self._sleep(self._config.poll.interval_seconds)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("[%s] recording failed", self.username)
        finally:
            self._recorder = None
            if self._slots is not None:
                self._slots.release()

    async def stop_recording(self) -> None:
        """Finalise an in-flight recording during shutdown."""
        if self._recorder is not None:
            await self._recorder.stop()

    def _next_interval(self) -> float:
        poll = self._config.poll
        if poll.jitter_seconds <= 0:
            return poll.interval_seconds
        return max(1.0, poll.interval_seconds + random.uniform(-poll.jitter_seconds, poll.jitter_seconds))

    async def _sleep(self, seconds: float) -> None:
        """Sleep, but wake immediately when shutdown is requested."""
        if seconds <= 0:
            return
        try:
            await asyncio.wait_for(self._shutdown.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
