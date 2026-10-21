# PR Webhook Harness

A small FastAPI service that receives GitHub webhooks, verifies them, and dispatches
`pull_request` events (by default only `opened`) to **pluggable handlers**.

```
GitHub ──webhook──▶ POST /webhook
                     │ 1. size limit (413)
                     │ 2. HMAC-SHA256 signature check, fail-closed (401)
                     │ 3. drop redelivered X-GitHub-Delivery ids
                     │ 4. filter: event == pull_request, action in PR_ACTIONS, drafts
                     │ 5. normalize → PullRequestEvent
                     ▼
               202 Accepted ──background──▶ Dispatcher
                                              ├─▶ handler A ┐ run concurrently,
                                              ├─▶ handler B ├ each with a timeout;
                                              └─▶ handler C ┘ failures are isolated
```

## Quick start

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env            # set GITHUB_WEBHOOK_SECRET
uvicorn prharness.main:app --factory --port 8080 --reload

# in another shell: send a signed sample "PR opened" event
GITHUB_WEBHOOK_SECRET=change-me python scripts/send_test_event.py
```

The server log shows the `log` handler output:

```
INFO prharness.handlers.log pull_request opened octo-org/octo-repo#42 by octocat: Add widget support (feature/widgets -> main) ...
```

## Connecting GitHub

1. Expose your local server, for example with [smee.io](https://smee.io):
   `npx smee-client --url https://smee.io/<channel> --target http://localhost:8080/webhook`
   (or deploy the Docker image somewhere public).
2. In the repo (or org), go to **Settings → Webhooks → Add webhook**:
   - Payload URL: your smee channel / public URL + `/webhook`
   - Content type: `application/json`
   - Secret: the same value as `GITHUB_WEBHOOK_SECRET`
   - Events: **Let me select individual events → Pull requests**
3. GitHub sends a `ping` (answered with `pong`). Open a PR to trigger your handlers.

A GitHub App webhook works the same way. `event.installation_id` is populated so
handlers can mint installation tokens.

## Configuration

All settings are environment variables (or `.env`):

| Variable | Default | Description |
| --- | --- | --- |
| `GITHUB_WEBHOOK_SECRET` | — (required) | Webhook secret; startup fails if empty |
| `HANDLERS` | `log` | Comma-separated handler specs (see below) |
| `PR_ACTIONS` | `opened` | Comma-separated `pull_request` actions to dispatch, `*` for all |
| `IGNORE_DRAFTS` | `false` | Skip draft PRs |
| `HANDLER_TIMEOUT_SECONDS` | `30` | Per-handler timeout |
| `FORWARD_URL` / `FORWARD_SECRET` | — | Used by the `forward` handler |
| `FORWARD_TIMEOUT_SECONDS` | `10` | HTTP timeout for `forward` |
| `MAX_WEBHOOK_BYTES` | `5000000` | Max request body |
| `WEBHOOK_REPLAY_TTL_SECONDS` | `600` | How long delivery ids are remembered |
| `WEBHOOK_REPLAY_CACHE_SIZE` | `10000` | Max remembered delivery ids |
| `LOG_LEVEL` | `INFO` | Python log level |

## Handlers

### Built-in

- **`log`**: logs a one-line summary of the PR.
- **`forward`**: POSTs `{"event": <summary>, "payload": <raw webhook>}` to `FORWARD_URL`.
  If `FORWARD_SECRET` is set, the body is signed in `X-PR-Harness-Signature-256`
  (`sha256=<hex hmac>`, the same scheme GitHub uses). Use it to fan events out to
  any other service, such as a CI trigger, a Slack relay or a serverless function.

### Writing your own

Subclass `prharness.Handler` and implement `handle` (either `async def` or a plain
`def`; sync handlers run in a worker thread):

```python
from prharness import Handler, PullRequestEvent


class Greeter(Handler):
    name = "greeter"
    actions = frozenset({"opened"})  # optional per-handler filter

    async def handle(self, event: PullRequestEvent) -> None:
        print(f"New PR {event.repo_full_name}#{event.number} by {event.author}")
```

`PullRequestEvent` exposes `action`, `number`, `title`, `html_url`, `author`,
`repo_full_name` (`owner`/`repo`), `base_ref`, `head_ref`, `head_sha`, `draft`,
`installation_id`, `delivery_id` and the full raw `payload`. Handlers receive the app
`Settings` in `__init__`, and can override `aclose()` to clean up on shutdown.

Each entry in `HANDLERS` is resolved, in order, as:

1. **`module:attr` path**, e.g. `HANDLERS=log,mypkg.handlers:Greeter`
2. **Registered name**, via `@prharness.registry.register("greeter")`
3. **Entry point** in the `prharness.handlers` group, so an installed package can ship handlers:

   ```toml
   [project.entry-points."prharness.handlers"]
   greeter = "mypkg.handlers:Greeter"
   ```

See [`examples/comment_handler.py`](examples/comment_handler.py) for a handler that
comments on each new PR through the GitHub API.

## Development

```bash
ruff check prharness tests examples scripts
pytest -q
```

## Docker

```bash
docker build -t pr-webhook-harness .
docker run -p 8080:8080 -e GITHUB_WEBHOOK_SECRET=... pr-webhook-harness
```

## Notes

- Handlers run in-process after the `202` response. Delivery is at-most-once, and a
  crash mid-dispatch loses the event. Use the `forward` handler to feed a durable
  queue if you need retries.
- The replay cache is in-memory and per-process.
