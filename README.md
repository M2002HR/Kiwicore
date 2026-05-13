# Kiwi Bridge

Kiwi Bridge is a channel-to-channel relay service:

1. Watches selected **Telegram channels**.
2. Downloads message media with a configurable per-message size limit.
3. Runs a per-route **guard script** (AI moderation).
4. Runs a per-channel Python script (only if guard allows).
5. Publishes channel-script outputs to mapped **Bale channels**.

The project is designed for production use with Docker Compose and for local development with auto-reload watch mode.

## Features

- Telegram channel watch via `getUpdates` (`channel_post`, `edited_channel_post`)
- Route mapping by source/destination username and/or channel ID
- Username-first matching and routing (with ID fallback)
- Per-route and global media size limits
- Local storage of raw updates, payloads, and downloaded media
- AI guard stage per route (`gaurd_script`) before script execution
- Single-stage channel script pipeline per route (`channel_script`)
- Default passthrough script for near 1:1 forwarding behavior
- Docker Compose integration with `gemini_server` (Gemini proxy submodule)
- Delivery to Bale as text/photo/video/voice/audio/document/animation/video_note
- Global sticker-block policy (stickers are never sent)
- Resilient polling loop with retry/backoff on network/API errors
- Dev watcher mode (`kiwi.dev`) and Dockerized production mode

## Project Structure

- `src/kiwi/service.py`: Main polling and orchestration loop
- `src/kiwi/config.py`: Environment and route loading
- `src/kiwi/platforms/client.py`: Telegram/Bale Bot API client wrapper
- `src/kiwi/platforms/parser.py`: Telegram channel update parser
- `src/kiwi/guard_runner.py`: Guard script execution + boolean parsing
- `src/kiwi/script_runner.py`: Channel script execution + output parsing
- `src/kiwi/dispatcher.py`: Bale message dispatching
- `src/kiwi/storage.py`: Local message/media storage
- `config/channels.json`: Runtime route configuration (ignored from git)
- `config/channels.example.json`: Versioned route template
- `scripts/channel_scripts/`: Channel scripts directory
- `scripts/gaurd_scrpts/`: Guard scripts directory
- `gemini_server/`: Git submodule (Gemini API proxy service)

## Route Configuration

Create your runtime file from the example:

```bash
cp config/channels.example.json config/channels.json
```

Route fields (per item):

- `name`: Route label
- `enabled`: Enable/disable route
- `source_channel_username`: Source Telegram channel username (preferred)
- `source_channel_id`: Source Telegram channel ID (fallback)
- `destination_channel_username`: Destination Bale channel username (preferred)
- `destination_channel_id`: Destination Bale channel ID (fallback)
- `gaurd_script`: Guard script filename under `scripts/gaurd_scrpts/` (default: `default_guard.py`)
- `channel_script`: Script filename under `scripts/channel_scripts/`
- `max_message_mb`: Optional per-route message media limit

Routing behavior:

- Source matching priority: `source_channel_username` -> `source_channel_id`
- Destination target priority: `destination_channel_username` -> `destination_channel_id`

## Channel Scripts

All scripts must be under:

- `scripts/channel_scripts/`

Default channel script included:

- `scripts/channel_scripts/default_channel_script.py`
- `scripts/channel_scripts/football.py` (AI football post generator + passthrough media)

### Script Contract

Each script is executed with:

- `--payload <path>`: Input message payload JSON
- `--input-dir <path>`: Downloaded input files directory
- `--output-dir <path>`: Script output directory

A script must return JSON via `stdout` (or `output.json` in `output-dir`) in this shape:

```json
{
  "messages": [
    {"type": "text", "text": "hello"},
    {"type": "photo", "path": "out.jpg", "caption": "optional"},
    {"type": "video", "path": "clip.mp4"},
    {"type": "voice", "path": "voice.ogg"},
    {"type": "audio", "path": "audio.mp3"},
    {"type": "document", "path": "file.pdf"},
    {"type": "animation", "path": "anim.gif"},
    {"type": "video_note", "path": "video_note.mp4"}
  ]
}
```

`sticker` outputs are ignored by global policy and are never sent.

For file-based outputs, `path` may be relative to `output-dir` or `input-dir`, or absolute.

Football AI script environment knobs (`football.py`):

- `FOOTBALL_AI_ENABLED`
- `FOOTBALL_AI_ENDPOINT` (fallbacks to `SCRIPT_CLEAN_AI_ENDPOINT` / `GUARD_AI_ENDPOINT`)
- `FOOTBALL_AI_MODEL`
- `FOOTBALL_AI_TIMEOUT_SEC`
- `FOOTBALL_AI_RETRY_COUNT`
- `FOOTBALL_AI_TOTAL_BUDGET_SEC` (global time budget to avoid script timeout)
- `FOOTBALL_AI_FAIL_OPEN`
- `FOOTBALL_AI_MAX_IMAGES`

## Guard Scripts

All guard scripts must be under:

- `scripts/gaurd_scrpts/`

Default guard included:

- `scripts/gaurd_scrpts/default_guard.py`

Guard contract:

- Receives `--payload`, `--input-dir`, `--output-dir`
- Must print `true`/`false` (or `1`/`0`) to stdout
- `true` means continue to channel script
- `false` means block forwarding for that message

## Default Passthrough Script

`default_channel_script.py` forwards incoming content with minimal transformation:

- Pure text -> text output
- Media -> same media type output when supported
- Caption -> attached to the first caption-capable output media
- Unknown file-like media -> safely downgraded to `document`
- Stickers -> always dropped (never forwarded)

## Environment Variables

Copy example and fill values:

```bash
cp .env.example .env
```

Important variables:

- `TELEGRAM_BOT_TOKEN`: Required
- `BALE_BOT_TOKEN`: Required
- `CHANNELS_CONFIG_PATH`: Default `./config/channels.json`
- `CHANNEL_SCRIPTS_DIR`: Default `./scripts/channel_scripts`
- `GAURD_SCRIPTS_DIR`: Default `./scripts/gaurd_scrpts`
- `DEFAULT_MAX_MESSAGE_MB`: Global per-message media limit
- `SCRIPT_TIMEOUT_SEC`: Max channel script runtime
- `GAURD_SCRIPT_TIMEOUT_SEC`: Max guard script runtime
- `POLL_IDLE_SLEEP_SEC`: Delay when no updates
- `POLL_ERROR_SLEEP_SEC`: Base retry delay on polling errors
- `TELETHON_PROXY_URL`: Optional proxy for MTProto (Telethon), e.g. `socks5://127.0.0.1:1080` or `http://127.0.0.1:2080`

Guard AI (used by `default_guard.py`):

- `GUARD_AI_ENABLED`
- `GUARD_AI_ENDPOINT` (inside Compose: `http://gemini_server:8000/proxy/gemini`)
- `GUARD_AI_MODEL` (optional; if empty, Gemini proxy default model is used)
- `GUARD_AI_TIMEOUT_SEC`

### Proxy / Nekoray

If your network requires proxy for Telegram/Bale API access:

- Set `HTTP_TRUST_ENV=true`
- Set proxy envs (`HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, `NO_PROXY`)

For local Nekoray HTTP proxy example:

```env
HTTP_TRUST_ENV=true
HTTP_PROXY=http://127.0.0.1:2080
HTTPS_PROXY=http://127.0.0.1:2080
ALL_PROXY=http://127.0.0.1:2080
NO_PROXY=127.0.0.1,localhost
```

In this repository's Docker Compose setup, `kiwi` runs on host network mode.  
If your proxy is on the host machine, use:

```env
HTTP_PROXY=http://127.0.0.1:2080
HTTPS_PROXY=http://127.0.0.1:2080
ALL_PROXY=http://127.0.0.1:2080
TELETHON_PROXY_URL=http://127.0.0.1:2080
```

## Local Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
cp config/channels.example.json config/channels.json
PYTHONPATH=src python -m kiwi.dev
```

Watch mode restarts automatically on changes in `src`, `scripts`, `config`, `.env`, `pyproject.toml`, and `requirements.txt`.

## Production (Docker Compose)

Initialize submodules first:

```bash
git submodule update --init --recursive
```

```bash
docker compose up --build -d
```

Check status/logs:

```bash
docker compose ps
docker compose logs -f kiwi
```

Stop:

```bash
docker compose down
```

## Testing

```bash
source .venv/bin/activate
pytest -q
```

## Git Rules in This Repo

- `config/channels.json` is ignored (runtime/local config)
- `config/channels.example.json` is versioned
- `scripts/channel_scripts/` and `scripts/gaurd_scrpts/` are versioned

## Troubleshooting

- `409 Conflict: terminated by other getUpdates request`
  - Only one active bot polling instance should run at a time.
- Repeated network timeouts
  - Verify proxy settings and connectivity.
  - The service retries with backoff automatically.
- Script not found
  - Ensure `channel_script` exists under `CHANNEL_SCRIPTS_DIR`.
