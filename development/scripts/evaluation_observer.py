"""Passive development-only SSE timing and whole-machine sampling; no model strategy."""

import asyncio
import json
import subprocess
import time
from dataclasses import asdict
from pathlib import Path

import httpx
import psutil

from clearact_lite.providers.openai_compatible import OpenAICompatible
from clearact_lite.storage.snapshots import atomic_write

RECORD: Path | None = None
START = time.perf_counter()
ACTIVE = {"task": "parent"}


def save(name, data):
    atomic_write(RECORD / name, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))


def append(name, data):
    with (RECORD / name).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(data, ensure_ascii=False) + "\n")


def relative_time():
    return round(time.perf_counter() - START, 6)


class ObservedStream(httpx.AsyncByteStream):
    def __init__(self, stream, metric):
        self.stream, self.metric, self.pending = stream, metric, b""

    def frame(self, line):
        if not line.startswith(b"data:"):
            return
        value = line[5:].strip()
        if value == b"[DONE]":
            return
        try:
            chunk = json.loads(value)
        except (ValueError, UnicodeDecodeError):
            return
        now = time.perf_counter()
        if chunk.get("usage"):
            self.metric["service_usage"] = chunk["usage"]
        if chunk.get("timings"):
            self.metric["service_timings"] = chunk["timings"]
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            pieces = [delta.get("content") or "", delta.get("reasoning_content") or ""]
            for call in delta.get("tool_calls") or []:
                function = call.get("function") or {}
                pieces.extend([function.get("name") or "", function.get("arguments") or ""])
            nonempty = "".join(pieces)
            if nonempty:
                if self.metric.get("first_delta_at") is None:
                    self.metric["first_delta_at"] = now
                self.metric["last_delta_at"] = now
                self.metric["effective_delta_frames"] += 1
                self.metric["delta_utf8_bytes"] += len(nonempty.encode("utf-8"))
            if choice.get("finish_reason"):
                self.metric["observed_finish_reason"] = choice["finish_reason"]

    async def __aiter__(self):
        async for part in self.stream:
            self.pending += part
            while b"\n" in self.pending:
                line, self.pending = self.pending.split(b"\n", 1)
                self.frame(line.rstrip(b"\r"))
            yield part
        if self.pending:
            self.frame(self.pending)

    async def aclose(self):
        await self.stream.aclose()


class ObservedTransport(httpx.AsyncBaseTransport):
    def __init__(self, metric):
        self.inner, self.metric = httpx.AsyncHTTPTransport(), metric

    async def handle_async_request(self, request):
        self.metric["http_started_at"] = time.perf_counter()
        response = await self.inner.handle_async_request(request)
        self.metric["http_status"] = response.status_code
        self.metric["headers_at"] = time.perf_counter()
        response.stream = ObservedStream(response.stream, self.metric)
        return response

    async def aclose(self):
        await self.inner.aclose()


class ObservedProvider:
    def __init__(self, settings):
        self.settings, self.metrics = settings, []

    async def complete(self, messages, tools, on_delta):
        metric = {
            "id": len(self.metrics) + 1,
            "task": ACTIVE["task"],
            "started_at": time.perf_counter(),
            "elapsed_run_seconds": relative_time(),
            "message_count": len(messages),
            "tool_count": len(tools),
            "message_utf8_bytes": sum(
                len(json.dumps(m.to_dict(), ensure_ascii=False).encode("utf-8")) for m in messages
            ),
            "first_delta_at": None,
            "effective_delta_frames": 0,
            "delta_utf8_bytes": 0,
        }
        self.metrics.append(metric)
        try:
            result = await OpenAICompatible(self.settings, ObservedTransport(metric)).complete(
                messages, tools, on_delta
            )
            metric["usage"] = asdict(result.usage)
            metric["finish_reason"] = result.finish_reason
            metric["tool_calls"] = [
                {"name": c.name, "path": c.arguments.get("path")} for c in result.message.tool_calls
            ]
            return result
        except BaseException as exc:
            metric["error"] = type(exc).__name__ + ": " + str(exc)
            raise
        finally:
            ended = time.perf_counter()
            metric["request_seconds"] = round(ended - metric["started_at"], 6)
            first, last = metric.get("first_delta_at"), metric.get("last_delta_at")
            metric["ttft_seconds"] = round(first - metric["started_at"], 6) if first is not None else None
            metric["stream_seconds"] = round(last - first, 6) if first is not None and last > first else None
            output = metric.get("service_usage", {}).get("completion_tokens")
            metric["stream_output_tokens_per_second_approx"] = (
                round(output / metric["stream_seconds"], 3)
                if isinstance(output, int) and metric["stream_seconds"]
                else None
            )
            metric["request_output_tokens_per_second"] = (
                round(output / metric["request_seconds"], 3) if isinstance(output, int) else None
            )
            append("calls.jsonl", metric)
            print(
                "CALL",
                metric["id"],
                metric["task"],
                metric["request_seconds"],
                "ttft",
                metric["ttft_seconds"],
                "tps",
                metric["stream_output_tokens_per_second_approx"],
                metric.get("finish_reason"),
                metric.get("error", ""),
                flush=True,
            )


def observe(event):
    if event.kind == "agent.started":
        ACTIVE["task"] = event.payload["task_id"]
    elif event.kind == "agent.finished":
        ACTIVE["task"] = "parent"
    if event.kind in {
        "agent.started",
        "agent.finished",
        "capacity.continuation",
        "context.compacted",
        "file.written",
        "task.plan",
        "reliability.diagnosing",
        "reliability.blocked",
        "reliability.paused",
        "reliability.resolved",
    }:
        append("progress.jsonl", {"seconds": relative_time(), "kind": event.kind, "payload": event.payload})
        print("EVENT", round(relative_time(), 2), event.kind, event.payload.get("task_id", ""), flush=True)


async def telemetry(stop):
    while not stop.is_set():
        data = {
            "seconds": relative_time(),
            "cpu_percent": psutil.cpu_percent(),
            "ram_percent": psutil.virtual_memory().percent,
        }
        try:

            def sample():
                return subprocess.run(
                    [
                        "nvidia-smi",
                        "--query-gpu=utilization.gpu,memory.used,memory.total,power.draw",
                        "--format=csv,noheader,nounits",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )

            result = await asyncio.to_thread(sample)
            data["gpu_csv"] = result.stdout.strip() if result.returncode == 0 else None
        except Exception as exc:
            data["gpu_error"] = type(exc).__name__
        append("telemetry.jsonl", data)
        try:
            await asyncio.wait_for(stop.wait(), 5)
        except TimeoutError:
            pass
