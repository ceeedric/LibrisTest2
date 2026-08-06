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

Edit `.env` with your Proton WireGuard private key, and `config/config.yaml`
with your `hls_source` and usernames. Then:

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

## ProtonVPN

The recorder container has **no network stack of its own** — `network_mode:
service:gluetun` puts it inside the VPN container's namespace. If the tunnel
drops, gluetun's firewall blocks everything, so the recorder cannot leak
traffic to your real IP.

### WireGuard (default, recommended)

1. Go to <https://account.protonvpn.com> → **Downloads** → **WireGuard configuration**.
2. Create a config for any server, download it, and copy the `PrivateKey` value.
3. Put it in `.env` as `WIREGUARD_PRIVATE_KEY`.
4. Set `SERVER_COUNTRIES` to where you want to exit.

### OpenVPN

Set `VPN_TYPE=openvpn` in `.env` and fill `OPENVPN_USER` / `OPENVPN_PASSWORD`
with the credentials from **Account → OpenVPN/IKEv2 username** — these are not
your Proton login. Append `+pmp` to the username if you need port forwarding.

### Verify the tunnel

```bash
docker compose exec gluetun wget -qO- https://ipinfo.io/json
```

Because the recorder shares that namespace, this is also the recorder's IP.
On a free Proton plan add `FREE_ONLY: "yes"` to the gluetun environment.

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

**`ffmpeg: 403 Forbidden`.** The playlist needs the same headers the API call
used. Add them under `recording.input_headers` (commonly `Referer`, sometimes a
`Cookie`).

**Recordings cut short.** Raise `stall_timeout_seconds`, or check the gluetun
logs for tunnel reconnects — a VPN drop kills the HTTP connection.

**mp4 files won't play.** Use `container: mkv`, or remux afterwards:
`ffmpeg -i in.mkv -c copy out.mp4`.
