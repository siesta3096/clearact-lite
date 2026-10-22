import json
from dataclasses import replace

from clearact_lite.core.contracts import Event, Message, ToolSpec, validate_messages
from clearact_lite.core.errors import ContextLimit


class ContextBuilder:
    def __init__(self, context_tokens: int, output_tokens: int, excerpt_chars: int):
        self.context_tokens, self.output_tokens = context_tokens, output_tokens
        self.input_allowance = context_tokens - output_tokens
        self.excerpt_chars = excerpt_chars

    def build(
        self, events: list[Event], tools: list[ToolSpec], *, enforce_limit: bool = True
    ) -> tuple[list[Message], dict]:
        messages = []
        excerpts = []
        checkpoints = [e for e in events if e.kind == "context.compacted"]
        cutoff = checkpoints[-1].payload["archived_through"] if checkpoints else 0
        if cutoff:
            from clearact_lite.context.compaction import memory_index

            messages.append(Message("system", memory_index(events, cutoff)))
        for event in events:
            if event.kind != "message":
                continue
            message = Message.from_dict(event.payload)
            if event.id <= cutoff and message.role in {"assistant", "tool"}:
                continue
            if message.role == "tool" and len(message.content) > self.excerpt_chars:
                half = self.excerpt_chars // 2
                reference = f"\n[Excerpt: history_read event_id={event.id}; use offset/limit for full text]\n"
                message = replace(message, content=message.content[:half] + reference + message.content[-half:])
                excerpts.append(event.id)
            messages.append(message)
        continuations = [e for e in events if e.kind == "capacity.continuation"]
        last_message = max((e.id for e in events if e.kind == "message"), default=0)
        if continuations and continuations[-1].id > last_message:
            messages.append(Message("user", continuations[-1].payload["instruction"]))
        # Several local chat templates accept only one system message, at index zero.
        systems = [message.content for message in messages if message.role == "system"]
        if systems:
            messages = [Message("system", "\n\n".join(systems))] + [
                message for message in messages if message.role != "system"
            ]
        validate_messages(messages)
        encoded = json.dumps(
            {
                "messages": [m.to_dict() for m in messages],
                "tools": [vars(t) for t in tools],
            },
            ensure_ascii=False,
        )
        # UTF-8 bytes plus framing is a conservative estimate, not actual token usage.
        estimate = len(encoded.encode("utf-8")) + 128 * (len(messages) + len(tools))
        info = {
            "estimated_input_tokens": estimate,
            "projection_utf8_bytes": len(encoded.encode("utf-8")),
            "input_allowance": self.input_allowance,
            "excerpted_events": excerpts,
            "estimator": "utf8_bytes_plus_framing",
            "archived_through": cutoff,
            "reserved_output_tokens": self.output_tokens,
            "estimated_occupancy": (estimate + self.output_tokens) / self.context_tokens,
        }
        if enforce_limit and estimate > self.input_allowance:
            raise ContextLimit(f"Required context estimate {estimate} exceeds allowance {self.input_allowance}")
        return messages, info
