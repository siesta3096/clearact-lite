"""Request occupancy: reported input plus bounded additions, never cumulative usage."""

import hashlib
import json
import math
from dataclasses import asdict

from clearact_lite.core.contracts import RequestFootprint


def fingerprint(value) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def request_footprint(messages, tools, model_key="") -> RequestFootprint:
    return RequestFootprint(
        model_key, fingerprint([vars(t) for t in tools]), tuple(fingerprint(m.to_dict()) for m in messages)
    )


def estimate_input(events, messages, tools, model_key="") -> tuple[int, dict]:
    footprint = request_footprint(messages, tools, model_key)
    encoded = json.dumps(
        {"messages": [m.to_dict() for m in messages], "tools": [vars(t) for t in tools]}, ensure_ascii=False
    )
    raw_bytes = len(encoded.encode("utf-8"))
    bound = raw_bytes + 128 * (len(messages) + len(tools))
    details = {
        "request_footprint": asdict(footprint),
        "projection_utf8_bytes": raw_bytes,
        "unmeasured_byte_bound": bound,
        "estimator": "utf8_bound_without_matching_measurement",
        "measurement_event_id": None,
    }
    projections = {e.id: e.payload for e in events if e.kind == "context.projected"}
    for event in reversed(events):
        if event.kind != "model.returned":
            continue
        reported = event.payload.get("usage", {}).get("input_tokens")
        if type(reported) is not int or reported < 0:
            continue
        projection = projections.get(event.payload.get("context_event_id"), {})
        try:
            measured = RequestFootprint.from_dict(projection["request_footprint"])
        except (KeyError, TypeError):
            continue
        count = len(measured.message_hashes)
        matched = footprint.message_hashes[:count] == measured.message_hashes
        if (
            not matched
            and measured.message_hashes
            and projection.get("transient_tail_hash") == measured.message_hashes[-1]
        ):
            # The one-shot output-limit notice disappears after a complete reply.
            # Retain its measured cost in the upper estimate; do not subtract tokens.
            count -= 1
            matched = footprint.message_hashes[:count] == measured.message_hashes[:count]
        if measured.model_key != footprint.model_key or measured.tools_hash != footprint.tools_hash or not matched:
            continue
        additions = messages[count:]
        extra = 0
        for index, message in enumerate(additions):
            output = event.payload.get("usage", {}).get("output_tokens")
            if (
                index == 0
                and message.role == "assistant"
                and fingerprint(message.to_dict()) == event.payload.get("response_message_hash")
                and type(output) is int
                and output >= 0
            ):
                # Allow for template/framing differences when generated calls are serialized back.
                extra += math.ceil(output * 1.05) + 128
            else:
                extra += len(json.dumps(message.to_dict(), ensure_ascii=False).encode("utf-8")) + 128
        margin = max(128, math.ceil(reported * 0.02))
        details.update(
            estimator="reported_input_plus_bounded_delta",
            measurement_event_id=event.id,
            reported_input_tokens=reported,
            bounded_delta_tokens=extra,
            estimation_margin_tokens=margin,
        )
        return reported + extra + margin, details
    return bound, details
