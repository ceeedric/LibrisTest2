# LibrisRecorder

Polls a stream API for a list of usernames and records each one with `ffmpeg`
whenever it goes live. Designed to run as a container behind a ProtonVPN tunnel.

```
every N minutes:  GET <hls_source>/<username>
                       │
                  live? ├── no  → sleep, poll again
                        └── yes → ffmpeg -i <playlist> -map 0:v:0 -map 0:a:0? -c copy out.mkv
                                  └── ffmpeg exits (stream ended) → cooldown → poll again
```

Each username runs its own independent loop, so one offline stream never blocks
another.

## Quick start

```bash
cp .env.example .env && cp config/config.example.yaml config/config.yaml
```

Set `GLUETUN_CONTAINER` in `.env` to the name of your running gluetun container,
and put your `hls_source` and usernames in `config/config.yaml`. With gluetun
already up:

```bash
docker compose up -d --build
```

Recordings land in `./recordings/<username>/<username>_<date>_<time>.mkv`.

```bash
docker compose logs -f recorder
```

## Configuration

Everything lives in `config/config.yaml` — see
[config.example.yaml](config/config.example.yaml) for the fully commented set.
The two required fields:

```yaml
hls_source: "https://streams.example.com"
usernames: [user1, user2]
```

Any value can reference environment variables as `${VAR}` or `${VAR:-default}`,
so secrets stay in `.env` rather than in the config file.

### Finding the playlist URL in your API response

The recorder does not assume a response schema. By default it scans the JSON for
the first `.m3u8` URL it can find, preferring keys named `hls`, `hls_url`,
`playlist_url`, `stream_url`, `url`, or `src`.

If that guesses wrong, point it at the exact field:

```yaml
api:
  stream_url_field: "data.streams[0].hls_url"
```

To see what your API actually returns and what the recorder makes of it:

```bash
docker compose run --rm recorder --probe user1
```

That prints the raw response plus the resolved `online` / playlist URL, then
exits without recording. Drop the username to probe every configured user.

### Deciding whether a stream is live

In order:

1. An HTTP status in `api.offline_status_codes` (default `404, 204, 410`) → offline.
2. Any other non-2xx → transport error, backs off and retries.
3. `api.online_check` — optional, e.g. `field: "isLive"`, `equals: true`.
4. No playlist URL found in the payload → offline.

Otherwise the stream is recorded.

### Cookies

If the API or the playlist needs a logged-in session, drop a Netscape-format
`cookies.txt` next to your config — the same file `yt-dlp` and the "Get
cookies.txt" browser extensions export:

```yaml
cookies:
  file: "/config/cookies.txt"
  send_to_api: true
  send_to_ffmpeg: true
```

Since `./config` is already bind-mounted, put the file at
`config/cookies.txt` on the host and it appears at `/config/cookies.txt` in the
container. No compose change needed. It is gitignored, like `config.yaml`.

Cookies are applied to **both** the status API polls and every ffmpeg request
(the playlist *and* each segment). Details worth knowing:

- **Only cookies scoped to the target host are sent.** Credentials for
  unrelated domains in your export never reach the stream host or the API.
- **Expired cookies are dropped**, and startup logs how many. If all of them
  have expired you get a loud warning — that is the usual cause of a setup that
  worked yesterday and 403s today.
- **`#HttpOnly_` entries are honoured.** Python's stdlib parser silently skips
  these; a hand-rolled parser is used so session cookies aren't lost.
- **The file is re-read when it changes.** Export a fresh one over the old
  path and the next poll picks it up — no restart.
- Cookie values are **redacted from DEBUG logs**, so `--log-level DEBUG` output
  stays safe to paste.

Check what was loaded without recording anything:

```bash
docker compose run --rm recorder --probe user1
```

Note that cookies go via ffmpeg's `-headers`, not its `-cookies` option — the
latter applies its own domain matching and was observed dropping cookies
silently, which is why host scoping is done in-process instead.

### Recording behaviour

| Setting | Default | Purpose |
| --- | --- | --- |
| `container` | `mkv` | `mkv`/`ts` survive an unclean kill; `mp4` is written fragmented so it stays playable |
| `copy_codecs` | `true` | `-c copy`, no re-encode — near-zero CPU |
| `stall_timeout_seconds` | `180` | Restarts if the output file stops growing (silently dead stream) |
| `min_file_bytes` | `262144` | Deletes false-start files instead of leaving 4 KB stubs |
| `max_concurrent` | `0` | Cap simultaneous recordings; `0` is unlimited |
| `min_free_disk_gb` | `2` | Skips a recording rather than filling the disk |
| `max_duration_seconds` | `0` | `0` records until the stream ends |

On `docker compose stop`, the recorder sends `q` to each ffmpeg so files are
finalised properly before the container exits.

## ProtonVPN (external gluetun)

gluetun is **managed outside this project**. The recorder joins its network
namespace via `network_mode: "container:${GLUETUN_CONTAINER}"`, so the recorder
has no network stack of its own and cannot leak traffic to your real IP. If the
tunnel drops, gluetun's killswitch blocks everything.

Point `.env` at your VPN container:

```bash
docker ps --filter name=gluetun --format '{{.Names}}  {{.Status}}'
```

```ini
GLUETUN_CONTAINER=gluetun
```

Start gluetun first — Compose cannot start or order a container it does not
own. If gluetun is down, `docker compose up` fails with *cannot join network of
a non-running container*.

### The one rule to remember

**Recreating gluetun orphans the recorder.** A `container:` network reference is
bound to the namespace of the container that existed when the recorder was
created, so a plain `restart` will not reattach. After any `docker compose
up/down`, image pull, or config change on the VPN stack:

```bash
docker compose up -d --force-recreate recorder
```

### Startup ordering

There is deliberately no `depends_on` — Compose cannot gate on the health of a
service in another project. This is safe: gluetun's killswitch is what prevents
leaks, not the dependency. If the recorder starts before the tunnel is up, the
first polls fail, back off exponentially, and recover on their own. You'll see a
few `poll failed` warnings at boot and nothing worse.

### Verify the tunnel

```bash
docker exec libris-recorder python -c "import httpx;print(httpx.get('https://ipinfo.io/json').text)"
```

Run from inside the recorder itself, this proves what the recorder actually
sees — the shared namespace means it is also gluetun's IP. Confirm it is not
your home IP before leaving it running.

## Running without Docker

```bash
pip install -r requirements.txt
python -m librisrecorder --config config/config.yaml
```

Requires `ffmpeg` on `PATH`. Useful flags: `--check-config`, `--probe [user]`,
`--log-level DEBUG`.

## Adding or removing usernames

Edit `usernames` in `config/config.yaml`, then:

```bash
docker compose restart recorder
```

In-flight recordings are finalised before the restart, so nothing is corrupted.

## Troubleshooting

**Nothing ever records.** Run `--probe <user>` — usually either the URL shape is
wrong (`api.path_template`) or the playlist field wasn't found.

**`ffmpeg: 403 Forbidden`.** Usually a missing or stale session — see
[Cookies](#cookies). If cookies are already configured, the playlist may also
need a `Referer`; add it under `recording.input_headers`.

**Recordings cut short.** Raise `stall_timeout_seconds`, or check the gluetun
logs for tunnel reconnects — a VPN drop kills the HTTP connection.

**mp4 files won't play.** Use `container: mkv`, or remux afterwards:
`ffmpeg -i in.mkv -c copy out.mp4`.
