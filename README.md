# PR Webhook Harness

A small FastAPI service that receives GitHub webhooks, verifies them, and dispatches
`pull_request` events (by default `opened`, `synchronize`, `reopened` and `ready_for_review`)
to **pluggable handlers**.

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
| `HANDLERS` | `log` | Comma-separated handler specs; `a>b` runs `a` then `b` in sequence (see below) |
| `PR_ACTIONS` | `opened,synchronize,reopened,ready_for_review` | Comma-separated `pull_request` actions to dispatch, `*` for all |
| `IGNORE_DRAFTS` | `false` | Skip draft PRs |
| `HANDLER_TIMEOUT_SECONDS` | `30` | Per-handler timeout (handlers may override, e.g. `security_review`) |
| `FORWARD_URL` / `FORWARD_SECRET` | — | Used by the `forward` handler |
| `FORWARD_TIMEOUT_SECONDS` | `10` | HTTP timeout for `forward` |
| `MAX_WEBHOOK_BYTES` | `5000000` | Max request body |
| `WEBHOOK_REPLAY_TTL_SECONDS` | `600` | How long delivery ids are remembered |
| `WEBHOOK_REPLAY_CACHE_SIZE` | `10000` | Max remembered delivery ids |
| `OPENAI_API_KEY` | — | Required by `code_review` and `security_review` |
| `OPENAI_MODEL` | `gpt-5.5` | Model used by `code_review` |
| `OPENAI_BASE_URL` | — | Optional OpenAI-compatible endpoint |
| `GITHUB_TOKEN` | — | Required by `code_review` and `security_review` (Pull requests: write, Contents: read) |
| `GITHUB_API_URL` | `https://api.github.com` | GitHub API base (for GHES) |
| `GITHUB_REVIEWER_LOGIN` | — (resolved via `GET /user`) | Login the reviews are posted as; set it for GitHub App tokens (e.g. `my-app[bot]`) to enable incremental re-reviews |
| `REVIEW_MAX_DIFF_CHARS` | `120000` | Diff is truncated beyond this |
| `REVIEW_MAX_FILE_READS` / `REVIEW_MAX_FILE_CHARS` | `10` / `40000` | Agent `read_file` budget |
| `REVIEW_MAX_TURNS` | `8` | Max LLM round-trips; the last turn forces `submit_review` |
| `REVIEW_MAX_COMMENTS` | `25` | Max findings posted (highest severity first) |
| `SECURITY_MODEL` | `gpt-5.5` | Model used by `security_review` (empty = `OPENAI_MODEL`) |
| `SECURITY_TIMEOUT_SECONDS` | `300` | Timeout for `security_review` (scan + agent) |
| `SECURITY_MAX_SCAN_FILES` | `50` | Max changed files fetched and scanned by Semgrep |
| `SEMGREP_ENABLED` | `true` | Run Semgrep in `security_review` |
| `SEMGREP_BIN` | `semgrep` | Semgrep executable |
| `SEMGREP_CONFIG` | `p/default` | Comma-separated Semgrep configs (registry rulesets or local rule files) |
| `SEMGREP_TIMEOUT_SECONDS` | `180` | Semgrep subprocess timeout |
| `LOG_LEVEL` | `INFO` | Python log level |

## Handlers

### Built-in

- **`log`**: logs a one-line summary of the PR.
- **`forward`**: POSTs `{"event": <summary>, "payload": <raw webhook>}` to `FORWARD_URL`.
  If `FORWARD_SECRET` is set, the body is signed in `X-PR-Harness-Signature-256`
  (`sha256=<hex hmac>`, the same scheme GitHub uses). Use it to fan events out to
  any other service, such as a CI trigger, a Slack relay or a serverless function.

- **`code_review`**: an AI code-quality reviewer (see below).
- **`security_review`**: an AI application-security peer reviewer backed by Semgrep and a secret scan (see below).

### AI code-quality review (`code_review`)

```bash
# .env  (gitignored; never commit real keys)
HANDLERS=log,code_review
PR_ACTIONS=opened,synchronize,reopened,ready_for_review
IGNORE_DRAFTS=true
OPENAI_API_KEY=sk-...
GITHUB_TOKEN=github_pat_...
```

For each PR it:
1. fetches the diff
2. runs an OpenAI tool-calling agent (`prharness/review/agent.py`), which may call `read_file` to read full files at the PR head and finishes by calling `submit_review` with a summary and findings (`path`, `line`, `severity`, `category`, `body`)
3. posts a single PR review of type `COMMENT`. The agent is read-only apart from that review: it never pushes commits, edits files, approves or requests changes

Findings on lines visible in the diff become inline comments, and the rest are listed in the review body. If GitHub rejects the inline comments, the review is re-posted as a summary only. The agent focuses on bugs, error handling, readability, maintainability, design, performance and tests. It treats PR content as untrusted data. Edit `prharness/review/prompt.py` to tune the review.

Secrets are only read from the environment (or `.env`) and are held as pydantic `SecretStr`, so they don't show up in reprs or logs. In production, set them through your platform's secret store, for example `fly secrets set OPENAI_API_KEY=...` or `docker run -e OPENAI_API_KEY`.

### Re-reviews on push

On `opened` the AI reviewers review the whole PR. On later events (`synchronize`, `reopened`,
`ready_for_review`) each reviewer finds its own last review on the PR and reviews only the
commits since then (`compare/<last-reviewed>...<head>`, limited to files in the PR so merges
from the base branch aren't reviewed):

- its earlier inline comments are given to the model as "already reported", and an identical
  finding at the same line is dropped before posting;
- inline comments are only placed on lines changed by the new commits;
- if the head was already reviewed (redelivery, `reopened` with no new commits) it does nothing;
- after a force-push/rebase (the old commit is no longer an ancestor), or if it never reviewed
  the PR, it reviews the whole PR again, still skipping its earlier comments;
- runs for the same PR are serialized, so two quick pushes don't produce overlapping reviews.

Own reviews are recognised by the review header and the token's login. With a GitHub App
installation token, `GET /user` isn't available, so set `GITHUB_REVIEWER_LOGIN`; otherwise
every event triggers a full review.

### AI security peer review (`security_review`)

```bash
HANDLERS=log,code_review>security_review   # security runs after code quality finishes
```

`a>b` runs handlers in sequence. Each one keeps its own timeout, and `b` still runs if `a` fails.

For each PR it:
1. fetches the diff and runs a secret scan over the added lines (AWS, GitHub, OpenAI, Stripe, Slack and Google key formats, plus private-key blocks). Matched values are never echoed.
2. fetches the changed files at the PR head and runs [Semgrep](https://semgrep.dev) (`SEMGREP_CONFIG`, default `p/default`) on them in a temporary directory, with `--metrics=off`. Rules are downloaded from the registry, but code is scanned locally. Only hits on added lines are kept.
3. gives the diff and the scanner hits to an OpenAI agent (`prharness/review/security_prompt.py`). The agent triages each hit, drops false positives, and adds its own findings across three groups:
   - SAST classes (OWASP Top 10 / CWE Top 25)
   - runtime issues a DAST scan would find that are visible in code (authn/authz, IDOR, CSRF, CORS, cookies/headers, open redirects, rate limiting)
   - issues static tools usually miss (business logic, race conditions, multi-step chains, OWASP LLM Top 10)
4. posts a separate comment-only review. Each finding has a severity (`critical`..`low`), category, CWE, `source` (`semgrep`, `secret-scan` or `llm`), the attack scenario and a fix. The review body reports what each scanner found.

It only reports security issues; code quality is left to `code_review`. Real DAST needs a running deployment, so it can't run against a PR, and runtime issues are only checked at the code level. If Semgrep isn't installed or fails, the review says so and continues with the LLM review only.

Semgrep is a CLI rather than a Python dependency, because its pins conflict with FastAPI's. Install it in its own environment, for example `pipx install semgrep` or `python -m venv ~/.semgrep && ~/.semgrep/bin/pip install semgrep`, then set `SEMGREP_BIN`. The Docker image includes it.

### AI dependency review (`dependency_review`)

A supply-chain reviewer that runs only on PRs that change a dependency manifest or lockfile
(`requirements*.txt`, `pyproject.toml`, `package.json`, `package-lock.json`, `poetry.lock`,
`uv.lock`, `go.mod`). It parses the file at base and head, and for each added or changed package:

- queries [OSV.dev](https://osv.dev) for known advisories. Exact pins are checked as-is, ranges at
  their lowest allowed version, unpinned packages at the latest release;
- checks the public registry (PyPI, npm, Go proxy) for packages that don't exist
  (typo/dependency confusion), packages first published < 90 days ago, npm install scripts,
  deprecation, and copyleft licenses;
- flags new names that are very close to a popular package (typosquats).

An OpenAI agent (`SECURITY_MODEL`) triages these hits and does its own pass. It groups advisories
per package with the fixed version to upgrade to, and also flags unpinned ranges, git/URL
dependencies and extra package indexes. Findings are posted as a separate comment-only review on
the manifest lines. OSV and the registries need no API key. Set `OSV_API_URL`, `PYPI_URL`,
`NPM_REGISTRY_URL` or `GO_PROXY_URL` to point at mirrors. `DEPENDENCY_MAX_PACKAGES` (default
`100`) caps the packages checked per run, direct dependencies first.

### AI CI/CD & infrastructure security review (`infra_review`)

A security reviewer for GitHub Actions workflows and `action.yml`, Dockerfiles, compose files,
Terraform/HCL and Kubernetes manifests (YAML containing `apiVersion` and `kind`). It runs only
when a PR touches one of these. Two scanners check the lines the PR adds:

- Semgrep with `INFRA_SEMGREP_CONFIG` (default
  `p/github-actions,p/dockerfile,p/terraform,p/kubernetes`);
- built-in checks, so there are still leads if Semgrep is missing:
  - GitHub Actions: `pull_request_target`/`workflow_run` checking out PR code, `${{ github.event.* }}` script injection, actions not pinned to a SHA, `write-all` or write-scoped tokens on untrusted triggers, a missing `permissions:` block
  - containers: root user, unpinned base image, secrets in `ENV`/`ARG`, `curl | sh`, `ADD <url>`
  - Terraform: `0.0.0.0/0`, public ACLs or access, disabled encryption
  - Kubernetes: privileged, host namespaces, privilege escalation, UID 0

An OpenAI agent (`SECURITY_MODEL`) triages the hits, merges duplicates between the two scanners,
and does its own pass, for example wildcard IAM, OIDC trust or `secrets: inherit`. It posts a
separate comment-only review. A scanner tag is kept only if that scanner actually flagged the
line, the same check `security_review` uses.

### AI test coverage review (`test_review`)

A static test reviewer. It never runs the PR's code: running untrusted code inside the harness
would expose its API keys. For each changed source file it lists:

- the functions the PR adds;
- the functions it modifies, taken from the enclosing-function context in each diff hunk;
- candidate tests: repository test files whose name mentions the module.

An OpenAI agent (`OPENAI_MODEL`) reads those tests and comments on added or modified behavior that
no test exercises: new functions, error paths, edge cases and bug fixes without a regression test.
Each comment proposes a concrete test with its name, inputs and expected result. It also flags
weak or flaky tests added in the PR. It posts a separate comment-only review and skips PRs that
change no source code.

### AI breaking-change review (`compat_review`)

Flags changes that can break existing callers, clients, data or deployments. It runs when a PR
touches source code, migrations or API schemas (OpenAPI/GraphQL/protobuf). Deterministic leads
from the diff are given to an OpenAI agent (`OPENAI_MODEL`), which verifies them and looks for
more:

- removed public symbols and changed signatures;
- removed or added endpoints (FastAPI/Flask/Express/Go routes);
- risky migrations: drops, renames, type changes, `NOT NULL` without a default, non-concurrent
  index builds;
- lines removed from API schemas;
- new environment variables, checked against `.env.example` and the README to find undocumented
  ones, and removed environment variables.

Each comment suggests a backward-compatible alternative, such as deprecating first, adding a
default, or an expand/contract migration. It posts a separate comment-only review.

### AI PR hygiene review (`hygiene_review`)

Reviews the PR as a whole rather than the code. It checks:

- whether the description matches what the diff actually does;
- whether unrelated changes are bundled together, and suggests a concrete split if so;
- size: oversized means more than `HYGIENE_MAX_CHANGED_LINES` (default 500) changed lines,
  excluding lockfiles and generated files, or more than `HYGIENE_MAX_FILES` (default 25) files;
- missing linked issues on non-trivial changes;
- WIP or fixup commits, and committed generated or binary files.

It posts one summary comment with at most 6 findings and no inline comments. It reviews each PR
once and does not review individual pushes.

### AI lead review (`lead_review`) and parallel groups

`lead_review` runs after the specialists and posts one comment for the author and the approver.
That comment gives:

- the overall risk of merging as-is (critical/high/medium/low);
- a numbered must-fix list;
- a should-fix list.

Duplicate findings across reviewers are merged, e.g. SQL injection reported by both code quality
and security. It reads only the `## AI ...` reviews this bot posted on the current head commit
and adds no new findings. It skips if there are none.

Run the specialists in parallel and the lead reviewer after them with a parenthesised group:

```env
HANDLERS=log,(code_review,security_review,dependency_review,infra_review,test_review,compat_review,hygiene_review)>lead_review
```

Top-level entries run concurrently, `a>b` runs in sequence, and `(a,b)` is a parallel group.
Groups can nest. Each handler keeps its own timeout, and a failure in one doesn't stop the
others.

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
