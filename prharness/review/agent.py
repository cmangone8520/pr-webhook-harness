"""Tool-calling OpenAI agent that reviews a PR diff for code quality."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from openai import AsyncOpenAI

from ..events import PullRequestEvent
from .prompt import READ_FILE_TOOL, SUBMIT_REVIEW_TOOL, SYSTEM_PROMPT

log = logging.getLogger("prharness.review.agent")

FileReader = Callable[[str], Awaitable[str | None]]


@dataclass(frozen=True)
class ReviewComment:
    path: str
    line: int
    severity: str
    category: str
    body: str


@dataclass(frozen=True)
class ReviewResult:
    summary: str
    comments: list[ReviewComment] = field(default_factory=list)


class CodeQualityAgent:
    def __init__(
        self,
        client: AsyncOpenAI,
        model: str,
        *,
        max_turns: int,
        max_file_reads: int,
        max_file_chars: int,
    ) -> None:
        self._client = client
        self._model = model
        self._max_turns = max_turns
        self._max_file_reads = max_file_reads
        self._max_file_chars = max_file_chars

    async def review(
        self,
        event: PullRequestEvent,
        diff: str,
        read_file: FileReader,
        *,
        diff_truncated: bool = False,
    ) -> ReviewResult:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _user_prompt(event, diff, diff_truncated)},
        ]
        file_reads = 0
        for turn in range(self._max_turns):
            last_turn = turn == self._max_turns - 1
            tools = [SUBMIT_REVIEW_TOOL] if last_turn else [READ_FILE_TOOL, SUBMIT_REVIEW_TOOL]
            tool_choice: Any = (
                {"type": "function", "function": {"name": "submit_review"}}
                if last_turn
                else "required"
            )
            response = await self._client.chat.completions.create(
                model=self._model, messages=messages, tools=tools, tool_choice=tool_choice
            )
            message = response.choices[0].message
            calls = message.tool_calls or []
            messages.append(_assistant_message(message.content, calls))
            if not calls:
                messages.append({"role": "user", "content": "Call submit_review now."})
                continue

            for call in calls:
                name = call.function.name
                args = _parse_args(call.function.arguments)
                if name == "submit_review":
                    return _to_result(args)
                if name == "read_file" and file_reads < self._max_file_reads:
                    file_reads += 1
                    path = str(args.get("path", ""))
                    log.info(
                        "agent read_file path=%s pr=%s#%d", path, event.repo_full_name, event.number
                    )
                    content = await read_file(path)
                    output = (
                        "File not found." if content is None else content[: self._max_file_chars]
                    )
                elif name == "read_file":
                    output = "File read budget exhausted; submit your review."
                else:
                    output = f"Unknown tool {name!r}."
                messages.append({"role": "tool", "tool_call_id": call.id, "content": output})

        raise RuntimeError("agent did not submit a review")


def _assistant_message(content: str | None, calls: list[Any]) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = [
            {
                "id": c.id,
                "type": "function",
                "function": {"name": c.function.name, "arguments": c.function.arguments},
            }
            for c in calls
        ]
    return message


def _user_prompt(event: PullRequestEvent, diff: str, truncated: bool) -> str:
    pr = event.payload.get("pull_request", {})
    description = (pr.get("body") or "").strip() or "(none)"
    note = "\nNOTE: the diff was truncated; review what is shown.\n" if truncated else ""
    return (
        f"Repository: {event.repo_full_name}\n"
        f"Pull request #{event.number}: {event.title}\n"
        f"Author: {event.author}\n"
        f"Branch: {event.head_ref} -> {event.base_ref}\n\n"
        f"<description>\n{description}\n</description>\n{note}\n"
        f"<diff>\n{diff}\n</diff>"
    )


def _parse_args(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _to_result(args: dict[str, Any]) -> ReviewResult:
    comments = []
    for item in args.get("comments") or []:
        try:
            comments.append(
                ReviewComment(
                    path=str(item["path"]),
                    line=int(item["line"]),
                    severity=str(item.get("severity", "low")),
                    category=str(item.get("category", "maintainability")),
                    body=str(item["body"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return ReviewResult(summary=str(args.get("summary", "")).strip(), comments=comments)
