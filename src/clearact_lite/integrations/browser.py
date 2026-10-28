import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname
from uuid import uuid4

from clearact_lite.core.ports import SnapshotStore, ToolContext
from clearact_lite.integrations.browser_diagnostics import BrowserDiagnostics
from clearact_lite.storage.snapshots import workspace_path

ELEMENTS = "a[href],button,input:not([type=hidden]),textarea,select,[role=button],[contenteditable=true]"


class BrowserManager:
    """One isolated, lazily opened browser shared by the user and model."""

    def __init__(self, workspace: Path, snapshots: SnapshotStore):
        self.workspace, self.snapshots = workspace, snapshots
        self.playwright = self.browser = self.context = self.page = None
        self.lock = asyncio.Lock()
        self.audit = workspace / ".clearact" / "browser" / "events.jsonl"
        self.diagnostics = BrowserDiagnostics(workspace)
        self._pending_context = None

    def _page_opened(self, page):
        self.page = page
        self.diagnostics.attach(page, self._pending_context)

    @property
    def is_open(self) -> bool:
        return self.browser is not None

    async def _ensure(self):
        if self.browser is None:
            from playwright.async_api import Error, async_playwright

            self.playwright = await async_playwright().start()
            try:
                if os.name == "nt":
                    try:
                        self.browser = await self.playwright.chromium.launch(channel="msedge", headless=True)
                    except Error:
                        self.browser = await self.playwright.chromium.launch(headless=True)
                else:
                    self.browser = await self.playwright.chromium.launch(headless=True)
                self.context = await self.browser.new_context(
                    viewport={"width": 1280, "height": 820},
                    accept_downloads=True,
                )
                self.context.on("page", self._page_opened)
                self.page = await self.context.new_page()
                self.page.set_default_timeout(15000)
            except BaseException:
                await self.aclose()
                raise
        if self.page is None or self.page.is_closed():
            self.page = self.context.pages[-1] if self.context.pages else await self.context.new_page()
        return self.page

    async def state(self) -> dict:
        if not self.is_open:
            return {"ready": False, "url": "", "title": "", "tabs": []}
        page = await self._ensure()
        return {
            "ready": True,
            "url": page.url,
            "title": await page.title(),
            "viewport": page.viewport_size,
            "tabs": [{"index": i, "url": p.url, "active": p == page} for i, p in enumerate(self.context.pages)],
            "diagnostics": self.diagnostics.state(page).context_view(),
        }

    async def _view(self, arguments: dict) -> dict:
        page = await self._ensure()
        text = await page.locator("body").inner_text() if await page.locator("body").count() else ""
        offset, limit = arguments.get("offset", 0), arguments.get("limit", 6000)
        elements = await page.locator(ELEMENTS).evaluate_all("""nodes => nodes.map((node, index) => ({
            ref: 'e' + (index + 1), tag: node.tagName.toLowerCase(),
            selector: node.id ? '#' + CSS.escape(node.id) : null,
            label: String(node.getAttribute('aria-label') || node.getAttribute('placeholder') ||
                node.innerText || node.getAttribute('title') || node.name || '').trim().slice(0,160),
            value: node.type === 'password' ? '' : String(node.value || '').slice(0,160),
            visible: !!(node.getBoundingClientRect().width && node.getBoundingClientRect().height)
        })).filter(node => node.visible)""")
        element_offset, element_limit = arguments.get("element_offset", 0), arguments.get("element_limit", 50)
        return {
            **await self.state(),
            "characters": len(text),
            "offset": offset,
            "text": text[offset : offset + limit],
            "element_count": len(elements),
            "elements": elements[element_offset : element_offset + element_limit],
        }

    def _locator(self, target: str):
        if target.startswith("e") and target[1:].isdigit():
            return self.page.locator(ELEMENTS).nth(int(target[1:]) - 1)
        return self.page.locator(target)

    def _url(self, value: str) -> str:
        if value == "about:blank":
            return value
        parsed = urlsplit(value)
        if parsed.scheme == "file":
            try:
                path = workspace_path(self.workspace, url2pathname(unquote(parsed.path)))
            except ValueError:
                raise ValueError(
                    "Local file URL is outside the chosen workspace. "
                    "Use its workspace-relative path, for example index.html, "
                    f"instead of guessing an absolute URL. Workspace: {self.workspace}"
                ) from None
            return path.as_uri()
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            return value
        candidate = (self.workspace / value).resolve()
        if candidate.is_file():
            return workspace_path(self.workspace, value).as_uri()
        if not parsed.scheme:
            return ("http://" if value.startswith(("localhost", "127.0.0.1")) else "https://") + value
        raise ValueError("Browser navigation supports HTTP(S), about:blank or a workspace file")

    def _record(self, arguments: dict, result: dict, context: ToolContext | None):
        payload = {
            "source": "model" if context else "user",
            "arguments": arguments,
            "result": result,
            "created_at": datetime.now(UTC).isoformat(),
        }
        self.audit.parent.mkdir(parents=True, exist_ok=True)
        with self.audit.open("a", encoding="utf-8") as log:
            log.write(json.dumps(payload, ensure_ascii=False) + "\n")
        if context:
            context.emit("browser.action", payload)

    async def control(self, arguments: dict, context: ToolContext | None = None) -> dict:
        async with self.lock:
            action = arguments["action"]
            self._pending_context = context
            try:
                async with asyncio.timeout(45):
                    if action == "close":
                        await self.aclose()
                        result = {"ready": False}
                    else:
                        await self._ensure()
                        self.diagnostics.set_owner(self.page, context)
                        result = await self._perform(action, arguments, context)
                        self.diagnostics.set_owner(self.page, context)
                self._record(arguments, result, context)
                return result
            except BaseException as exc:
                diagnostic = self.diagnostics.state(self.page).context_view()
                exc.browser_diagnostics = diagnostic
                self._record(
                    arguments,
                    {
                        "error": type(exc).__name__,
                        "detail": str(exc),
                        "outcome": "interrupted_or_failed",
                        "diagnostics": diagnostic,
                    },
                    context,
                )
                raise
            finally:
                self._pending_context = None

    async def _perform(self, action: str, args: dict, context: ToolContext | None) -> dict:
        page = self.page
        target, value = args.get("target", ""), args.get("value", "")
        extra = {}
        if action in {"open", "navigate"}:
            await page.goto(self._url(args.get("url") or value or "about:blank"), wait_until="domcontentloaded")
        elif action == "click":
            if target:
                await self._locator(target).click()
            else:
                await page.mouse.click(args["x"], args["y"])
        elif action == "fill":
            await self._locator(target).fill(value)
        elif action == "type":
            await page.keyboard.insert_text(value)
        elif action == "press":
            if target:
                await self._locator(target).press(value)
            else:
                await page.keyboard.press(value)
        elif action == "select":
            await self._locator(target).select_option(value)
        elif action in {"check", "uncheck"}:
            await self._locator(target).set_checked(action == "check")
        elif action == "hover":
            await self._locator(target).hover()
        elif action == "scroll":
            await page.mouse.wheel(args.get("x", 0), args.get("y", 600))
        elif action == "evaluate":
            extra["evaluation"] = await page.evaluate(value)
        elif action == "upload":
            paths = [str(workspace_path(self.workspace, path)) for path in args.get("files", [])]
            await self._locator(target).set_input_files(paths)
        elif action == "download":
            async with page.expect_download() as waiting:
                await self._locator(target).click()
            download = await waiting.value
            name = PureWindowsPath(download.suggested_filename).name
            from clearact_lite.storage.uploads import UploadStore

            temporary = self.workspace / ".clearact" / "browser" / uuid4().hex
            temporary.parent.mkdir(parents=True, exist_ok=True)
            await download.save_as(temporary)
            info = UploadStore(self.workspace).add(name, temporary.read_bytes())
            temporary.unlink()
            extra["download"] = info
        elif action == "back":
            await page.go_back(wait_until="domcontentloaded")
        elif action == "forward":
            await page.go_forward(wait_until="domcontentloaded")
        elif action == "reload":
            await page.reload(wait_until="domcontentloaded")
        elif action == "new_tab":
            self.page = await self.context.new_page()
            await self.page.goto(self._url(args.get("url") or "about:blank"), wait_until="domcontentloaded")
        elif action == "switch_tab":
            self.page = self.context.pages[args.get("index", 0)]
        elif action == "close_tab":
            await page.close()
            await self._ensure()
        elif action == "viewport":
            await page.set_viewport_size({"width": args["width"], "height": args["height"]})
        elif action == "wait":
            if target:
                await self._locator(target).wait_for(state="visible")
            else:
                await asyncio.sleep(args.get("milliseconds", 1000) / 1000)
        elif action == "assert_text":
            actual = await self._locator(target or "body").inner_text()
            if value not in actual:
                raise AssertionError(f"Expected text not present: {value!r}")
            extra["assertion"] = {"passed": True, "expected": value, "target": target or "body"}
        elif action == "screenshot":
            data = await page.screenshot(type="png", full_page=bool(args.get("full_page")))
            relative = Path("browser-captures") / f"{uuid4().hex}.png"
            if context:
                self.snapshots.write_bytes(self.workspace / relative, data, context.emit)
            else:
                from clearact_lite.storage.snapshots import atomic_write

                atomic_write(self.workspace / relative, data)
            extra["screenshot_path"] = relative.as_posix()
        elif action not in {"read", "tabs"}:
            raise ValueError("Unknown browser action")
        view = await self._view(args) if action in {"open", "read"} else await self.state()
        return {**view, **extra}

    async def screenshot(self) -> bytes:
        async with self.lock:
            if not self.is_open:
                raise ValueError("浏览器尚未启动")
            return await self.page.screenshot(type="jpeg", quality=80, timeout=5000)

    async def aclose(self) -> None:
        self.diagnostics.close()
        try:
            if self.browser is not None:
                await self.browser.close()
        finally:
            self.browser = self.context = self.page = None
            if self.playwright is not None:
                await self.playwright.stop()
                self.playwright = None
