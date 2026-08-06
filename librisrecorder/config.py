"""Configuration loading, ``${ENV}`` expansion and validation."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(ValueError):
    """Raised when the config file is missing required values or malformed."""


def _expand_env(value: Any) -> Any:
    """Recursively expand ``${VAR}`` / ``${VAR:-default}`` inside strings."""
    if isinstance(value, str):

        def replace(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            resolved = os.environ.get(name)
            if resolved is None:
                if default is None:
                    raise ConfigError(
                        f"config references ${{{name}}} but that environment "
                        f"variable is not set (use ${{{name}:-fallback}} to allow a default)"
                    )
                return default
            return resolved

        return _ENV_PATTERN.sub(replace, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


@dataclass
class PollConfig:
    interval_seconds: float = 300.0
    jitter_seconds: float = 30.0
    timeout_seconds: float = 15.0
    retry_backoff_seconds: float = 60.0
    max_backoff_seconds: float = 900.0
    stagger_start: bool = True


@dataclass
class OnlineCheck:
    """Optional extra assertion on the API payload before recording."""

    field_path: str | None = None
    equals: Any = None


@dataclass
class ApiConfig:
    base_url: str = ""
    path_template: str = "/{username}"
    headers: dict[str, str] = field(default_factory=dict)
    query: dict[str, str] = field(default_factory=dict)
    stream_url_field: str | None = None
    autodetect_m3u8: bool = True
    offline_status_codes: list[int] = field(default_factory=lambda: [404, 204, 410])
    online_check: OnlineCheck = field(default_factory=OnlineCheck)
    verify_tls: bool = True

    def url_for(self, username: str) -> str:
        path = self.path_template.format(username=username)
        return f"{self.base_url.rstrip('/')}/{path.lstrip('/')}"


@dataclass
class RecordingConfig:
    output_dir: str = "/recordings"
    path_template: str = "{username}/{username}_{date}_{time}.{ext}"
    container: str = "mkv"
    video_map: str = "0:v:0"
    audio_map: str | None = "0:a:0?"
    copy_codecs: bool = True
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
    input_headers: dict[str, str] = field(default_factory=dict)
    extra_input_args: list[str] = field(default_factory=list)
    extra_output_args: list[str] = field(default_factory=list)
    max_duration_seconds: float = 0.0
    min_file_bytes: int = 262_144
    cooldown_seconds: float = 15.0
    max_concurrent: int = 0
    min_free_disk_gb: float = 2.0
    stall_timeout_seconds: float = 180.0
    graceful_stop_seconds: float = 20.0
    ffmpeg_loglevel: str = "warning"
    ffmpeg_path: str = "ffmpeg"


@dataclass
class CookiesConfig:
    file: str | None = None
    send_to_api: bool = True
    send_to_ffmpeg: bool = True


@dataclass
class Config:
    usernames: list[str]
    api: ApiConfig
    poll: PollConfig
    recording: RecordingConfig
    cookies: CookiesConfig = field(default_factory=CookiesConfig)
    log_level: str = "INFO"


def _section(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key) or {}
    if not isinstance(value, dict):
        raise ConfigError(f"config section '{key}' must be a mapping, got {type(value).__name__}")
    return value


def _known_keys(section: dict[str, Any], allowed: set[str], name: str) -> None:
    unknown = set(section) - allowed
    if unknown:
        raise ConfigError(f"unknown key(s) in '{name}': {', '.join(sorted(unknown))}")


def _identity() -> str:
    """Describe the running user, so ownership problems are self-evident."""
    try:
        return f"uid={os.getuid()} gid={os.getgid()}"
    except AttributeError:  # Windows
        return "uid=n/a"


def _describe_parent(parent: Path) -> str:
    """Explain what the config's directory actually looks like from in here."""
    if not parent.exists():
        return (
            f"its directory {parent} does not exist — the volume is probably not "
            f"mounted (expected something like './config:{parent}:ro')"
        )
    if not parent.is_dir():
        return f"{parent} exists but is not a directory"
    try:
        entries = sorted(p.name for p in parent.iterdir())
    except PermissionError:
        return (
            f"{parent} exists but this process ({_identity()}) may not read it — "
            f"check ownership/permissions on the host directory, and SELinux "
            f"labelling (try the ':z' mount flag) if applicable"
        )
    listing = ", ".join(entries) if entries else "<empty>"
    return f"{parent} contains: {listing}"


def load_config(path: str | Path) -> Config:
    """Read a YAML config file and return a validated :class:`Config`."""
    path = Path(path)

    try:
        exists = path.exists()
    except PermissionError:
        exists = False

    if not exists:
        raise ConfigError(f"config file not found: {path} ({_describe_parent(path.parent)})")
    if not path.is_file():
        raise ConfigError(f"{path} is not a regular file")

    try:
        text = path.read_text(encoding="utf-8")
    except PermissionError as exc:
        raise ConfigError(
            f"{path} exists but cannot be read by this process ({_identity()}): {exc}"
        ) from exc
    except OSError as exc:
        raise ConfigError(f"could not read {path}: {exc}") from exc

    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError("config root must be a mapping")
    raw = _expand_env(raw)

    base_url = raw.get("hls_source")
    if not base_url or not isinstance(base_url, str):
        raise ConfigError("'hls_source' is required and must be the API base URL string")
    if not base_url.startswith(("http://", "https://")):
        raise ConfigError(f"'hls_source' must start with http:// or https:// (got {base_url!r})")

    usernames = raw.get("usernames") or []
    if not isinstance(usernames, list) or not all(isinstance(u, str) and u for u in usernames):
        raise ConfigError("'usernames' must be a non-empty list of strings")
    # Preserve order, drop duplicates.
    usernames = list(dict.fromkeys(usernames))
    if not usernames:
        raise ConfigError("'usernames' must contain at least one entry")

    api_raw = _section(raw, "api")
    _known_keys(
        api_raw,
        {
            "path_template",
            "headers",
            "query",
            "stream_url_field",
            "autodetect_m3u8",
            "offline_status_codes",
            "online_check",
            "verify_tls",
        },
        "api",
    )
    online_raw = _section(api_raw, "online_check")
    _known_keys(online_raw, {"field", "equals"}, "api.online_check")

    api = ApiConfig(
        base_url=base_url,
        path_template=api_raw.get("path_template", ApiConfig.path_template),
        headers={str(k): str(v) for k, v in (api_raw.get("headers") or {}).items()},
        query={str(k): str(v) for k, v in (api_raw.get("query") or {}).items()},
        stream_url_field=api_raw.get("stream_url_field"),
        autodetect_m3u8=bool(api_raw.get("autodetect_m3u8", True)),
        offline_status_codes=list(api_raw.get("offline_status_codes") or [404, 204, 410]),
        online_check=OnlineCheck(
            field_path=online_raw.get("field"),
            equals=online_raw.get("equals"),
        ),
        verify_tls=bool(api_raw.get("verify_tls", True)),
    )
    if "{username}" not in api.path_template:
        raise ConfigError("'api.path_template' must contain the {username} placeholder")
    if not api.stream_url_field and not api.autodetect_m3u8:
        raise ConfigError(
            "set 'api.stream_url_field' or leave 'api.autodetect_m3u8' enabled, "
            "otherwise the playlist URL can never be found"
        )

    poll_raw = _section(raw, "poll")
    _known_keys(
        poll_raw,
        {
            "interval_seconds",
            "jitter_seconds",
            "timeout_seconds",
            "retry_backoff_seconds",
            "max_backoff_seconds",
            "stagger_start",
        },
        "poll",
    )
    poll = PollConfig(
        interval_seconds=float(poll_raw.get("interval_seconds", PollConfig.interval_seconds)),
        jitter_seconds=float(poll_raw.get("jitter_seconds", PollConfig.jitter_seconds)),
        timeout_seconds=float(poll_raw.get("timeout_seconds", PollConfig.timeout_seconds)),
        retry_backoff_seconds=float(
            poll_raw.get("retry_backoff_seconds", PollConfig.retry_backoff_seconds)
        ),
        max_backoff_seconds=float(poll_raw.get("max_backoff_seconds", PollConfig.max_backoff_seconds)),
        stagger_start=bool(poll_raw.get("stagger_start", True)),
    )
    if poll.interval_seconds <= 0:
        raise ConfigError("'poll.interval_seconds' must be greater than 0")
    if poll.jitter_seconds < 0:
        raise ConfigError("'poll.jitter_seconds' must be 0 or greater")

    rec_raw = _section(raw, "recording")
    _known_keys(rec_raw, {f.name for f in RecordingConfig.__dataclass_fields__.values()}, "recording")
    recording = RecordingConfig(
        **{
            key: rec_raw[key]
            for key in RecordingConfig.__dataclass_fields__
            if key in rec_raw and rec_raw[key] is not None
        }
    )
    recording.max_duration_seconds = float(recording.max_duration_seconds)
    recording.min_file_bytes = int(recording.min_file_bytes)
    recording.cooldown_seconds = float(recording.cooldown_seconds)
    recording.max_concurrent = int(recording.max_concurrent)
    recording.min_free_disk_gb = float(recording.min_free_disk_gb)
    recording.stall_timeout_seconds = float(recording.stall_timeout_seconds)
    recording.graceful_stop_seconds = float(recording.graceful_stop_seconds)
    if "{username}" not in recording.path_template:
        raise ConfigError("'recording.path_template' must contain the {username} placeholder")

    cookies_raw = _section(raw, "cookies")
    _known_keys(cookies_raw, {"file", "send_to_api", "send_to_ffmpeg"}, "cookies")
    cookies = CookiesConfig(
        file=cookies_raw.get("file") or None,
        send_to_api=bool(cookies_raw.get("send_to_api", True)),
        send_to_ffmpeg=bool(cookies_raw.get("send_to_ffmpeg", True)),
    )
    if cookies.file:
        cookie_path = Path(cookies.file)
        if not cookie_path.is_file():
            # Failing here beats silently polling without credentials and
            # spending an hour wondering why everything returns 403.
            raise ConfigError(
                f"cookies.file is set to {cookie_path} but no readable file is there "
                f"({_describe_parent(cookie_path.parent)})"
            )
        if not (cookies.send_to_api or cookies.send_to_ffmpeg):
            raise ConfigError(
                "cookies.file is set but both send_to_api and send_to_ffmpeg are false"
            )

    logging_raw = _section(raw, "logging")
    _known_keys(logging_raw, {"level"}, "logging")

    return Config(
        usernames=usernames,
        api=api,
        poll=poll,
        recording=recording,
        cookies=cookies,
        log_level=str(logging_raw.get("level", "INFO")).upper(),
    )
