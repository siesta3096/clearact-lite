import asyncio
import json

import httpx

from clearact_lite.core.contracts import Message, ModelReply, ToolCall, ToolSpec, Usage
from clearact_lite.core.errors import ProtocolError
from clearact_lite.core.ports import Delta
from clearact_lite.settings import Settings


def wire_message(message: Message) -> dict:
    result = {"role": message.role, "content": message.content}
    if message.tool_calls:
        result["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                },
            }
            for call in message.tool_calls
        ]
    if message.tool_call_id:
        result["tool_call_id"] = message.tool_call_id
    if message.reasoning_content:
        result["reasoning_content"] = message.reasoning_content
    return result


def reported_usage(raw: dict) -> Usage:
    def count(key: str) -> int | None:
        value = raw.get(key)
        return value if type(value) is int and value >= 0 else None

    return Usage(count("prompt_tokens"), count("completion_tokens"))


class OpenAICompatible:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.transport = transport

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        on_delta: Delta,
    ) -> ModelReply:
        settings = self.settings
        payload = {
            "model": settings.model,
            "messages": [wire_message(m) for m in messages],
            "max_tokens": settings.max_output_tokens,
            "temperature": settings.temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            payload["tools"] = [{"type": "function", "function": vars(tool)} for tool in tools]
        if settings.reasoning_effort is not None:
            payload["reasoning_effort"] = settings.reasoning_effort
        headers = {"Authorization": f"Bearer {settings.api_key}"} if settings.api_key else {}
        content, reasoning, calls = [], [], {}
        usage, finish, chunks = {}, None, []

        async def receive() -> None:
            nonlocal finish, usage
            async with asyncio.timeout(settings.request_timeout_seconds):
                async with httpx.AsyncClient(
                    timeout=settings.request_timeout_seconds,
                    trust_env=False,
                    transport=self.transport,
                ) as client:
                    async with client.stream(
                        "POST",
                        settings.base_url.rstrip("/") + "/chat/completions",
                        json=payload,
                        headers=headers,
                    ) as response:
                        response.raise_for_status()
                        async for line in response.aiter_lines():
                            if not line.startswith("data:"):
                                continue
                            data = line[5:].strip()
                            if data == "[DONE]":
                                break
                            # Retain malformed frames too; errors must remain inspectable.
                            try:
                                chunk = json.loads(data)
                            except ValueError:
                                chunks.append({"invalid_data": data})
                                raise
                            chunks.append(chunk)
                            if "error" in chunk:
                                raise ProtocolError("Model service returned a stream error")
                            if chunk.get("usage"):
                                usage = chunk["usage"]
                            choices = chunk.get("choices") or []
                            if not choices:
                                continue
                            choice = choices[0]
                            delta = choice.get("delta", {})
                            if delta.get("content"):
                                content.append(delta["content"])
                                on_delta(delta["content"])
                            if delta.get("reasoning_content"):
                                reasoning.append(delta["reasoning_content"])
                            for call in delta.get("tool_calls") or []:
                                target = calls.setdefault(call["index"], {"id": "", "name": "", "arguments": ""})
                                target["id"] += call.get("id") or ""
                                function = call.get("function") or {}
                                target["name"] += function.get("name") or ""
                                target["arguments"] += function.get("arguments") or ""
                            finish = choice.get("finish_reason") or finish

        try:
            await receive()
        except BaseException as exc:
            exc.raw_response = {"chunks": chunks}
            raise
        raw = {"chunks": chunks}
        try:
            if finish is None:
                raise ProtocolError("Model stream ended without a finish reason")
            parsed = (
                ()
                if finish in {"length", "max_tokens", "max_output_tokens"}
                else tuple(ToolCall(c["id"], c["name"], json.loads(c["arguments"])) for _, c in sorted(calls.items()))
            )
            message = Message("assistant", "".join(content), parsed, reasoning_content="".join(reasoning) or None)
            return ModelReply(message, finish, reported_usage(usage), raw)
        except (ValueError, TypeError, ProtocolError) as exc:
            error = ProtocolError(f"Invalid model response: {exc}")
            error.raw_response = raw
            raise error from exc
