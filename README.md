# Kiwicore

**A resilient Telegram-to-Bale channel relay with configurable content processing, AI moderation, media delivery, deduplication, retries, and review workflows.**

Kiwicore watches selected Telegram channels, stores incoming payloads and media, runs route-specific guard and transformation scripts, and publishes approved outputs to mapped Bale channels.

## Engineering highlights

- Configurable source-to-destination route mapping
- Text and media relay across Telegram and Bale
- Route-specific moderation and transformation scripts
- AI-assisted guard integration through a Gemini proxy service
- SQLite-backed delivery ledger and review queue
- Redis-backed due queue and route-level concurrency locks
- Idempotency and deduplication across retries
- Exponential backoff for transient platform or network failures
- Per-route media-size limits
- Local auto-reload development mode and Docker Compose deployment
- Optional MySQL ledger backend

## Technology

`Python` · `Telegram Bot API` · `Bale Bot API` · `Redis` · `SQLite` · `MySQL` · `Docker Compose` · `Async HTTP` · `AI Moderation`

## Processing pipeline

```text
Telegram channel update
          │
          ▼
Parse and persist payload/media
          │
          ▼
Route lookup and deduplication
          │
          ▼
Guard script / AI moderation
          │
     blocked ──► audit result
          │ approved
          ▼
Channel transformation script
          │
          ▼
Normalized output messages
          │
          ▼
Bale delivery
          │
          ├── success ─► ledger checkpoint
          └── ambiguous/transient failure ─► retry or review queue
```

## Supported output types

Transformation scripts can produce:

- text
- photo
- video
- voice
- audio
- document
- animation
- video note

Stickers are blocked by global policy. Unknown file-like media is safely downgraded to document delivery when appropriate.

## Repository structure

```text
src/kiwi/
├── service.py              # Polling and orchestration
├── config.py               # Environment and route configuration
├── platforms/              # Telegram/Bale clients and parsers
├── guard_runner.py         # Guard-script execution
├── script_runner.py        # Transformation-script execution
├── dispatcher.py           # Bale delivery
└── storage.py              # Payload and media persistence

config/channels.example.json
scripts/channel_scripts/
scripts/gaurd_scrpts/       # Legacy directory spelling retained by the codebase
gemini_server/              # Gemini proxy submodule
```

The existing `gaurd_*` names contain a historical spelling error and remain documented only for compatibility with the current configuration contract. New code and prose should use the term **guard**.

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
cp config/channels.example.json config/channels.json
```

Start local development with auto-reload:

```bash
PYTHONPATH=src python -m kiwi.dev
```

Run the containerized stack:

```bash
git submodule update --init --recursive
docker compose up --build -d
docker compose ps
docker compose logs -f kiwi
```

## Route configuration

Each route can define:

- source Telegram username and/or numeric ID
- destination Bale username and/or numeric ID
- enabled state
- guard-script name
- transformation-script name
- media-size limit

Username matching is preferred, with numeric IDs used as fallback.

Runtime route files are ignored from Git. Keep only sanitized examples in version control.

## Script contract

A transformation script receives:

```text
--payload <message-json>
--input-dir <downloaded-media>
--output-dir <generated-files>
```

It returns a JSON object containing normalized output messages. File paths may be relative to the input or output directory, or absolute when explicitly permitted by deployment policy.

Guard scripts receive the same context and emit a boolean decision. Guard execution is timeout-bounded and runs before transformation.

## Delivery reliability

Kiwicore uses several layers to reduce duplicate or lost delivery:

- persistent source-message ledger
- Redis due queue
- retry scheduling with backoff
- route-level in-flight limit
- lock TTL for abandoned work
- explicit review state for ambiguous delivery outcomes
- checkpoint updates after confirmed success

A route-level concurrency of one is recommended when strict source ordering is required.

## Configuration

Runtime settings are supplied through `.env` and include:

- platform tokens
- route and script paths
- media-size and script-timeout limits
- polling and retry timing
- Redis queue settings
- SQLite or MySQL ledger DSN
- proxy configuration
- AI moderation endpoint and model
- review-alert destination

Use placeholder credentials in examples, for example:

```env
SYNC_LEDGER_DSN=mysql://app_user:CHANGE_ME@127.0.0.1:3306/kiwi_sync
```

Never publish real database passwords, bot tokens, proxy credentials, or channel identifiers.

## Proxy support

HTTP and SOCKS proxy settings are configurable for restricted network environments. For containers that must reach a proxy running on the host, use the platform-appropriate host address rather than embedding machine-specific values in the repository.

## Verification

```bash
pytest -q
docker compose config --quiet
```

Real platform delivery should be tested with controlled channels and non-sensitive media. A successful API response alone should not be treated as proof of end-to-end delivery; inspect the destination and the ledger state.

## Security and privacy

- Store platform and provider credentials only in untracked environment files.
- Restrict downloaded media and raw update storage.
- Validate script names and prevent path traversal.
- Apply strict timeouts and resource limits to external scripts.
- Redact message content and tokens from operational logs.
- Treat source posts, media, channel mappings, and review items as potentially sensitive.

## Project status

Kiwicore demonstrates cross-platform integration, event processing, scriptable automation, queue-based retries, idempotency, media handling, AI moderation, and containerized operations.