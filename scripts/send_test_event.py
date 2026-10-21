"""Sign and POST a sample webhook to a running harness.

Usage: python scripts/send_test_event.py [--url URL] [--event pull_request] [payload.json]
Reads the signing secret from GITHUB_WEBHOOK_SECRET.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys
import uuid

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from prharness.signature import sign  # noqa: E402

DEFAULT_PAYLOAD = pathlib.Path(__file__).resolve().parents[1] / "examples/pull_request_opened.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("payload", nargs="?", default=str(DEFAULT_PAYLOAD))
    parser.add_argument("--url", default="http://localhost:8080/webhook")
    parser.add_argument("--event", default="pull_request")
    args = parser.parse_args()

    secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
    if not secret:
        print("GITHUB_WEBHOOK_SECRET is not set", file=sys.stderr)
        return 1
    body = pathlib.Path(args.payload).read_bytes()
    response = httpx.post(
        args.url,
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": args.event,
            "X-GitHub-Delivery": str(uuid.uuid4()),
            "X-Hub-Signature-256": sign(secret, body),
        },
    )
    print(response.status_code, response.text)
    return 0 if response.is_success else 1


if __name__ == "__main__":
    raise SystemExit(main())
