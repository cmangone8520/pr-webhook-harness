import json

import httpx
import respx

from prharness import PullRequestEvent
from prharness.handlers.forward import SIGNATURE_HEADER, ForwardHandler
from prharness.signature import verify_signature


@respx.mock
async def test_forwards_signed_payload(settings, sample_payload) -> None:
    settings.forward_url = "https://sink.example/hook"
    settings.forward_secret = "fwd"
    route = respx.post("https://sink.example/hook").mock(return_value=httpx.Response(200))
    handler = ForwardHandler(settings)

    await handler.handle(PullRequestEvent.from_payload(sample_payload, "d1"))
    await handler.aclose()

    request = route.calls.last.request
    body = json.loads(request.content)
    assert body["event"]["number"] == 42
    assert body["event"]["repository"] == "octo-org/octo-repo"
    assert body["payload"] == sample_payload
    assert request.headers["X-GitHub-Delivery"] == "d1"
    assert verify_signature("fwd", request.content, request.headers[SIGNATURE_HEADER])
