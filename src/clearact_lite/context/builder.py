from dataclasses import replace

from clearact_lite.context.budget import estimate_input
from clearact_lite.core.contracts import Event, Message, ToolSpec, validate_messages
from clearact_lite.core.errors import ContextLimit


class ContextBuilder:
    def __init__(self, context_tokens: int, output_tokens: int, excerpt_chars: int, model_key: str = ""):
        self.context_tokens, self.output_tokens = context_tokens, output_tokens
        self.model_key = model_key
        self.input_allowance = context_tokens - output_tokens
        self.excerpt_chars = excerpt_chars

    def build(
        self, events: list[Event], tools: list[ToolSpec], *, enforce_limit: bool = True
    ) -> tuple[list[Message], dict]:
        messages = []
        excerpts = []
        checkpoints = [e for e in events if e.kind == "context.compacted"]
        cutoff = checkpoints[-1].payload["archived_through"] if checkpoints else 0
        retained = set(checkpoints[-1].payload.get("retained_message_ids", [])) if checkpoints else set()
        archive_text = None
        restored_at = max((e.id for e in events if e.kind == "file.restored"), default=0)
        if cutoff or restored_at:
            from clearact_lite.context.compaction import memory_index

            checkpoint = checkpoints[-1] if checkpoints else None
            as_of = (
                checkpoint.payload.get("index_as_of", checkpoint.id or max(e.id for e in events)) if checkpoint else 0
            )
            # A manual restore changes workspace state outside model messages.
            # Refresh once at that event, then keep the measured prefix stable.
            as_of = max(as_of, restored_at)
            archive_text = memory_index([e for e in events if e.id <= as_of], cutoff, retained)
        for event in events:
            if event.kind != "message":
                continue
            message = Message.from_dict(event.payload)
            if event.id <= cutoff and event.id not in retained and message.role in {"assistant", "tool"}:
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
        transient = bool(continuations and continuations[-1].id > last_message)
        # Several local chat templates accept only one system message, at index zero.
        systems = [message.content for message in messages if message.role == "system"]
        if archive_text:
            systems.append(archive_text)
        if systems:
            messages = [Message("system", "\n\n".join(systems))] + [
                message for message in messages if message.role != "system"
            ]
        validate_messages(messages)
        estimate, basis = estimate_input(events, messages, tools, self.model_key)
        info = {
            **basis,
            "estimated_input_tokens": estimate,
            "input_allowance": self.input_allowance,
            "excerpted_events": excerpts,
            "archived_through": cutoff,
            "reserved_output_tokens": self.output_tokens,
            "estimated_occupancy": (estimate + self.output_tokens) / self.context_tokens,
            "transient_tail_hash": basis["request_footprint"]["message_hashes"][-1] if transient else None,
        }
        if enforce_limit and estimate > self.input_allowance:
            raise ContextLimit(f"Required context estimate {estimate} exceeds allowance {self.input_allowance}")
        return messages, info
