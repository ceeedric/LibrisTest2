"""Builds and supervises the ffmpeg process that writes one recording."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import RecordingConfig

log = logging.getLogger(__name__)

_UNSAFE_PATH_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class DiskSpaceError(RuntimeError):
    """Raised when free space is below ``recording.min_free_disk_gb``."""


@dataclass
class RecordingResult:
    path: Path
    exit_code: int | None
    duration_seconds: float
    size_bytes: int
    kept: bool


def sanitize(component: str) -> str:
    """Make a username safe to use as a path component."""
    cleaned = _UNSAFE_PATH_CHARS.sub("_", component).strip(". ")
    return cleaned or "unknown"


def build_output_path(config: RecordingConfig, username: str, when: datetime) -> Path:
    relative = config.path_template.format(
        username=sanitize(username),
        date=when.strftime("%Y-%m-%d"),
        time=when.strftime("%H-%M-%S"),
        timestamp=when.strftime("%Y%m%d-%H%M%S"),
        epoch=int(when.timestamp()),
        ext=config.container.lstrip("."),
    )
    return Path(config.output_dir) / relative


def build_command(config: RecordingConfig, url: str, output: Path) -> list[str]:
    """Assemble the ffmpeg argv for a single recording."""
    command = [
        config.ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        config.ffmpeg_loglevel,
        "-y",
    ]

    if config.user_agent:
        command += ["-user_agent", config.user_agent]
    if config.input_headers:
        headers = "".join(f"{key}: {value}\r\n" for key, value in config.input_headers.items())
        command += ["-headers", headers]

    # Survive brief network hiccups mid-stream rather than ending the recording.
    command += [
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_on_network_error", "1",
        "-reconnect_delay_max", "10",
        "-rw_timeout", "20000000",
    ]
    command += [str(arg) for arg in config.extra_input_args]
    command += ["-i", url]

    command += ["-map", config.video_map]
    if config.audio_map:
        command += ["-map", config.audio_map]
    if config.copy_codecs:
        command += ["-c", "copy"]
    if config.max_duration_seconds > 0:
        command += ["-t", str(config.max_duration_seconds)]

    container = config.container.lstrip(".").lower()
    if container == "mp4":
        # Keeps a partially written mp4 playable if the process dies uncleanly.
        command += ["-movflags", "+frag_keyframe+empty_moov+default_base_moof"]
    elif container in {"mkv", "matroska"}:
        command += ["-f", "matroska"]
    elif container == "ts":
        command += ["-f", "mpegts"]

    command += [str(arg) for arg in config.extra_output_args]
    command.append(str(output))
    return command


def check_disk_space(config: RecordingConfig) -> None:
    target = Path(config.output_dir)
    if config.min_free_disk_gb <= 0:
        return
    probe = target if target.exists() else target.parent
    free_gb = shutil.disk_usage(probe).free / 1_000_000_000
    if free_gb < config.min_free_disk_gb:
        raise DiskSpaceError(
            f"only {free_gb:.2f} GB free at {probe}, "
            f"minimum is {config.min_free_disk_gb:.2f} GB"
        )


class FfmpegRecorder:
    """Runs one ffmpeg invocation and watches it until the stream ends."""

    def __init__(self, config: RecordingConfig, username: str) -> None:
        self._config = config
        self._username = username
        self._process: asyncio.subprocess.Process | None = None

    async def run(self, url: str) -> RecordingResult:
        config = self._config
        check_disk_space(config)

        started = datetime.now()
        output = build_output_path(config, self._username, started)
        output.parent.mkdir(parents=True, exist_ok=True)

        command = build_command(config, url, output)
        log.info("[%s] recording to %s", self._username, output)
        log.debug("[%s] ffmpeg argv: %s", self._username, " ".join(command))

        self._process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        process = self._process

        stderr_task = asyncio.create_task(self._drain_stderr(process))
        stall_task: asyncio.Task[None] | None = None
        if config.stall_timeout_seconds > 0:
            stall_task = asyncio.create_task(self._watch_for_stall(output))

        try:
            exit_code = await process.wait()
        except asyncio.CancelledError:
            await self.stop()
            with contextlib.suppress(asyncio.CancelledError):
                await process.wait()
            raise
        finally:
            if stall_task:
                stall_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stall_task
            with contextlib.suppress(asyncio.CancelledError):
                await stderr_task
            self._process = None

        duration = (datetime.now() - started).total_seconds()
        size = output.stat().st_size if output.exists() else 0

        kept = True
        if size < config.min_file_bytes:
            kept = False
            log.warning(
                "[%s] discarding %s (%d bytes after %.1fs, below min_file_bytes=%d)",
                self._username, output.name, size, duration, config.min_file_bytes,
            )
            with contextlib.suppress(OSError):
                output.unlink()
        else:
            log.info(
                "[%s] finished %s (%.1f MB, %.1f min, ffmpeg exit %s)",
                self._username, output.name, size / 1_048_576, duration / 60, exit_code,
            )

        return RecordingResult(output, exit_code, duration, size, kept)

    async def stop(self) -> None:
        """Ask ffmpeg to finalise the file, escalating to SIGTERM then SIGKILL."""
        process = self._process
        if process is None or process.returncode is not None:
            return

        log.info("[%s] stopping ffmpeg gracefully", self._username)
        if process.stdin and not process.stdin.is_closing():
            with contextlib.suppress(Exception):
                process.stdin.write(b"q")
                await process.stdin.drain()

        for signal_step in (None, "terminate", "kill"):
            if signal_step == "terminate":
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
            elif signal_step == "kill":
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
            try:
                await asyncio.wait_for(
                    asyncio.shield(process.wait()),
                    timeout=max(self._config.graceful_stop_seconds, 1.0),
                )
                return
            except asyncio.TimeoutError:
                log.warning("[%s] ffmpeg did not exit, escalating", self._username)

    async def _drain_stderr(self, process: asyncio.subprocess.Process) -> None:
        if process.stderr is None:
            return
        async for raw in process.stderr:
            line = raw.decode("utf-8", "replace").rstrip()
            if not line:
                continue
            level = logging.WARNING if re.search(r"error|failed|invalid", line, re.I) else logging.DEBUG
            log.log(level, "[%s] ffmpeg: %s", self._username, line)

    async def _watch_for_stall(self, output: Path) -> None:
        """Kill ffmpeg if the output file stops growing — a silently dead stream."""
        timeout = self._config.stall_timeout_seconds
        interval = max(min(timeout / 3, 30.0), 5.0)
        last_size = -1
        stalled_for = 0.0

        while True:
            await asyncio.sleep(interval)
            size = output.stat().st_size if output.exists() else 0
            if size > last_size:
                last_size = size
                stalled_for = 0.0
                continue
            stalled_for += interval
            if stalled_for >= timeout:
                log.warning(
                    "[%s] output has not grown in %.0fs, stopping ffmpeg",
                    self._username, stalled_for,
                )
                await self.stop()
                return
