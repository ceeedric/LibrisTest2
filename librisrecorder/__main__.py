"""Entrypoint: wires config, workers and signal handling together."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
from pathlib import Path

import httpx

from . import __version__
from .api import StreamApi, build_client
from .config import Config, ConfigError, load_config
from .cookies import CookieStore
from .worker import UserWorker

log = logging.getLogger("librisrecorder")


def setup_logging(level: str) -> None:
    resolved = getattr(logging, level, logging.INFO)
    logging.basicConfig(
        level=resolved,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    # basicConfig is a no-op on the second call; set the level directly so the
    # config-file value still wins over the bootstrap default.
    logging.getLogger().setLevel(resolved)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="librisrecorder",
        description="Poll a stream API for live usernames and record them with ffmpeg.",
    )
    parser.add_argument(
        "-c", "--config",
        default=os.environ.get("LIBRIS_CONFIG", "/config/config.yaml"),
        help="path to the YAML config (env: LIBRIS_CONFIG)",
    )
    parser.add_argument(
        "--check-config", action="store_true",
        help="validate the config and exit",
    )
    parser.add_argument(
        "--probe", metavar="USERNAME", nargs="?", const="*",
        help="poll once (all users, or one) and print what the API returned, then exit",
    )
    parser.add_argument("--log-level", help="override logging.level from the config")
    parser.add_argument("--version", action="version", version=f"librisrecorder {__version__}")
    return parser.parse_args(argv)


async def probe(config: Config, target: str) -> int:
    """One-shot diagnostic poll — useful for pinning down stream_url_field."""
    usernames = config.usernames if target == "*" else [target]
    exit_code = 0

    cookies = CookieStore(
        config.cookies.file,
        send_to_api=config.cookies.send_to_api,
        send_to_ffmpeg=config.cookies.send_to_ffmpeg,
    )
    if cookies.enabled:
        loaded = cookies.load()
        live = [c for c in loaded if not c.expired]
        print(f"cookies: {len(loaded)} loaded from {cookies.path}, {len(live)} unexpired")
        if loaded and not live:
            print("  WARNING: every cookie has expired — export a fresh cookies.txt")
    else:
        print("cookies: not configured")

    async with build_client(config.api, config.poll.timeout_seconds) as client:
        api = StreamApi(config.api, client, cookies=cookies)
        for username in usernames:
            url = config.api.url_for(username)
            print(f"\n=== {username} ===")
            print(f"GET {url}")
            try:
                response = await client.get(
                    url,
                    headers=config.api.headers or None,
                    params=config.api.query or None,
                    cookies=cookies.jar(),
                )
                print(f"HTTP {response.status_code}")
                body = response.text.strip()
                try:
                    print(json.dumps(response.json(), indent=2)[:4000])
                except ValueError:
                    print(body[:2000] or "<empty body>")

                status = await api.check(username)
                print(f"-> online={status.online} reason={status.reason}")
                if status.url:
                    print(f"-> playlist: {status.url}")
                elif status.online:
                    exit_code = 1
            except httpx.HTTPError as exc:
                print(f"request failed: {exc}")
                exit_code = 1
    return exit_code


async def run(config: Config) -> int:
    Path(config.recording.output_dir).mkdir(parents=True, exist_ok=True)

    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, AttributeError):
            loop.add_signal_handler(sig, shutdown.set)

    slots = (
        asyncio.Semaphore(config.recording.max_concurrent)
        if config.recording.max_concurrent > 0
        else None
    )

    cookies = CookieStore(
        config.cookies.file,
        send_to_api=config.cookies.send_to_api,
        send_to_ffmpeg=config.cookies.send_to_ffmpeg,
    )
    cookies.load()

    async with build_client(config.api, config.poll.timeout_seconds) as client:
        api = StreamApi(config.api, client, cookies=cookies)
        workers = [
            UserWorker(username, config, api, slots, shutdown, cookies=cookies)
            for username in config.usernames
        ]

        log.info(
            "watching %d username(s) at %s every %.0fs -> %s",
            len(workers), config.api.base_url,
            config.poll.interval_seconds, config.recording.output_dir,
        )

        tasks = [asyncio.create_task(w.run(), name=f"worker:{w.username}") for w in workers]
        await shutdown.wait()

        log.info("shutdown requested — finalising recordings")
        await asyncio.gather(*(w.stop_recording() for w in workers), return_exceptions=True)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    log.info("stopped")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging((args.log_level or "INFO").upper())

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        log.error("config error: %s", exc)
        return 2

    setup_logging(args.log_level.upper() if args.log_level else config.log_level)

    if args.check_config:
        log.info(
            "config OK: %d username(s), base %s, output %s",
            len(config.usernames), config.api.base_url, config.recording.output_dir,
        )
        return 0

    try:
        if args.probe:
            return asyncio.run(probe(config, args.probe))
        return asyncio.run(run(config))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
