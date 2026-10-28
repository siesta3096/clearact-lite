"""Passive browser error receipts. No task checks, extra page evaluations, retries or reloads."""

import json
from collections import OrderedDict
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from clearact_lite.core.contracts import BrowserDiagnostic, BrowserPageState


def diagnostic_url(value):
    parsed = urlsplit(value)
    return urlunsplit((parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, "", ""))


@dataclass
class PageRecord:
    document_id: int
    owner: object = None
    ready_state: str = "loading"
    navigation_status: int | None = None
    errors: OrderedDict = field(default_factory=OrderedDict)
    omitted: int = 0


class BrowserDiagnostics:
    def __init__(self, workspace):
        self.path = workspace / ".clearact/browser/diagnostics.jsonl"
        self.session_id = uuid4().hex[:12]
        self.sequence = self.document_id = 0
        self.pages = {}
        self.closed = False

    def attach(self, page, owner=None):
        self.closed = False
        self.document_id += 1
        self.pages[page] = PageRecord(self.document_id, owner)
        page.on("request", lambda request: self.navigation(page, request))
        page.on("domcontentloaded", lambda: self.ready(page, "interactive"))
        page.on("load", lambda: self.ready(page, "complete"))
        page.on(
            "pageerror",
            lambda error: self.capture(
                page,
                "javascript_error",
                str(error),
                url=page.url,
                stack=getattr(error, "stack", "") or "",
            ),
        )
        page.on("console", lambda message: self.console(page, message))
        page.on(
            "requestfailed",
            lambda request: self.capture(
                page,
                "resource_failed",
                request.failure or "Request failed",
                url=request.url,
                resource_type=request.resource_type,
            ),
        )
        page.on("response", lambda response: self.response(page, response))
        page.on("close", lambda: self.pages.pop(page, None))

    def set_owner(self, page, owner):
        if page in self.pages:
            self.pages[page].owner = owner

    def navigation(self, page, request):
        if request.is_navigation_request() and request.frame == page.main_frame and page in self.pages:
            self.document_id += 1
            self.pages[page] = PageRecord(self.document_id, self.pages[page].owner)

    def ready(self, page, state):
        if page in self.pages:
            self.pages[page].ready_state = state

    def console(self, page, message):
        if message.type == "error":
            location = message.location
            self.capture(
                page,
                "console_error",
                message.text,
                url=location.get("url", ""),
                line_number=location.get("lineNumber"),
                column_number=location.get("columnNumber"),
            )

    def response(self, page, response):
        if page not in self.pages:
            return
        request = response.request
        if request.is_navigation_request() and request.frame == page.main_frame:
            self.pages[page].navigation_status = response.status
        if response.status >= 400:
            self.capture(
                page,
                "http_error",
                f"HTTP {response.status} {response.status_text}",
                url=response.url,
                resource_type=request.resource_type,
                http_status=response.status,
            )

    def capture(self, page, kind, message, **metadata):
        if self.closed or page not in self.pages:
            return
        record = self.pages[page]
        self.sequence += 1
        metadata["url"] = diagnostic_url(metadata.get("url", ""))
        diagnostic = BrowserDiagnostic(self.session_id, self.sequence, record.document_id, kind, message, **metadata)
        if record.owner:
            event = record.owner.emit("browser.diagnostic", asdict(diagnostic))
            diagnostic = replace(diagnostic, event_id=event.id)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps({**asdict(diagnostic), "created_at": datetime.now(UTC).isoformat()}, ensure_ascii=False)
                + "\n"
            )
        # Repeated errors retain their first reference in model context. Full occurrences stay above.
        # Thus a new timestamp/counter cannot disguise an unchanged observation as progress.
        key = (
            kind,
            message,
            metadata.get("url"),
            metadata.get("http_status"),
            metadata.get("line_number"),
            metadata.get("column_number"),
        )
        if key not in record.errors:
            record.errors[key] = diagnostic
            if len(record.errors) > 64:
                record.errors.popitem(last=False)
                record.omitted += 1

    def state(self, page):
        record = self.pages.get(page)
        if record is None:
            return BrowserPageState(self.session_id)
        errors = tuple(record.errors.values())
        return BrowserPageState(
            self.session_id,
            record.document_id,
            record.ready_state,
            record.navigation_status,
            errors[-8:],
            record.omitted + max(0, len(errors) - 8),
        )

    def close(self):
        self.closed = True
        self.pages.clear()
