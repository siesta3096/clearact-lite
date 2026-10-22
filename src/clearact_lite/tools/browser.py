import json

from clearact_lite.core.contracts import ToolResult, ToolSpec
from clearact_lite.core.ports import BrowserPort, ToolContext
from clearact_lite.tools.files import object_schema

READ_FIELDS = {
    "offset": {"type": "integer", "minimum": 0},
    "limit": {"type": "integer", "minimum": 1, "maximum": 12000},
    "element_offset": {"type": "integer", "minimum": 0},
    "element_limit": {"type": "integer", "minimum": 1, "maximum": 100},
}
ACTIONS = [
    "navigate",
    "click",
    "fill",
    "type",
    "press",
    "select",
    "check",
    "uncheck",
    "hover",
    "scroll",
    "evaluate",
    "upload",
    "download",
    "back",
    "forward",
    "reload",
    "tabs",
    "new_tab",
    "switch_tab",
    "close_tab",
    "viewport",
    "wait",
    "assert_text",
    "screenshot",
    "close",
]


class BrowserTool:
    def __init__(self, browser: BrowserPort):
        self.browser = browser

    async def execute(self, arguments: dict, context: ToolContext) -> ToolResult:
        data = await self.browser.control({"action": self.action, **arguments}, context)
        return ToolResult(json.dumps(data, ensure_ascii=False))


class OpenBrowser(BrowserTool):
    action = "open"
    spec = ToolSpec(
        "browser_open",
        "Open HTTP(S) or a workspace HTML file in the shared browser. Returns page "
        "text and interactive elements. For local files use a workspace-relative path such as index.html; "
        "do not invent an absolute OS path. HTTP localhost is supported.",
        object_schema({"url": {"type": "string"}}, ["url"]),
    )


class ReadBrowser(BrowserTool):
    action = "read"
    spec = ToolSpec(
        "browser_read",
        "Read current browser page text and element refs with pagination. "
        "Refs like e3 are valid for the current page; CSS selectors are also accepted.",
        object_schema(READ_FIELDS, []),
    )


class ActBrowser(BrowserTool):
    action = ""
    spec = ToolSpec(
        "browser_action",
        "Act in the shared browser. target is CSS or element ref (e3); value is "
        "text/key/option/JavaScript. click without target uses x,y. upload uses workspace file paths. "
        "download clicks target and saves the download. assert_text explicitly checks text; "
        "screenshot saves a PNG and returns its path. Returns compact state; use browser_read "
        "to inspect page content. No task approval layer.",
        object_schema(
            {
                "action": {"type": "string", "enum": ACTIONS},
                "target": {"type": "string"},
                "value": {"type": "string"},
                "url": {"type": "string"},
                "x": {"type": "number"},
                "y": {"type": "number"},
                "files": {"type": "array", "items": {"type": "string"}},
                "index": {"type": "integer", "minimum": 0},
                "width": {"type": "integer", "minimum": 1},
                "height": {"type": "integer", "minimum": 1},
                "milliseconds": {"type": "integer", "minimum": 0},
                "full_page": {"type": "boolean"},
                **READ_FIELDS,
            },
            ["action"],
        ),
    )
