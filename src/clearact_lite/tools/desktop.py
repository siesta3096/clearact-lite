"""Opt-in Windows desktop actions for the current interactive session."""

import asyncio
import ctypes
import importlib
import json
import sys
from uuid import uuid4

from clearact_lite.core.contracts import ToolResult, ToolSpec
from clearact_lite.storage.snapshots import workspace_path
from clearact_lite.tools.files import object_schema


class DesktopAction:
    spec = ToolSpec(
        "desktop_action",
        "Operate the real Windows desktop on its primary monitor. Observe returns windows and bounds; "
        "screenshot saves the primary screen to a workspace PNG (the model receives its path, not pixels). "
        "focus takes a window ID from observe. Move/click take both x and y. "
        "press takes keys such as ['ctrl','a']; type takes printable ASCII text.",
        object_schema(
            {
                "action": {"enum": ["observe", "screenshot", "focus", "move", "click", "press", "type"]},
                "window_id": {"type": "integer", "minimum": 1},
                "x": {"type": "integer", "minimum": 0},
                "y": {"type": "integer", "minimum": 0},
                "keys": {
                    "type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1, "maxItems": 4
                },
                "text": {"type": "string", "maxLength": 500, "pattern": "^[ -~]*$"},
            },
            ["action"],
        ),
    )

    async def execute(self, arguments, context):
        if sys.platform != "win32":
            return ToolResult("Desktop control requires Windows", True)
        if importlib.util.find_spec("pyautogui") is None:
            return ToolResult("Install the desktop extra: python -m pip install -e .[desktop]", True)
        result = await asyncio.to_thread(self.act, arguments, context.workspace)
        return ToolResult(json.dumps(result, ensure_ascii=False))

    @staticmethod
    def act(arguments, workspace):
        gui = importlib.import_module("pyautogui")
        action = arguments["action"]
        gui.failSafeCheck()
        if action == "observe":
            windows = gui.getAllWindows()
            active = gui.getActiveWindow()
            return {
                "screen_size": list(gui.size()),
                "cursor": list(gui.position()),
                "active_window": active.title if active else None,
                "windows": [
                    {
                        "id": window._hWnd,
                        "title": window.title,
                        "bounds": [window.left, window.top, window.width, window.height],
                    }
                    for window in windows
                    if window.title
                ][:60],
            }
        if action == "screenshot":
            path = workspace / "desktop-captures" / f"{uuid4().hex}.png"
            path = workspace_path(workspace, str(path))
            path.parent.mkdir(exist_ok=True)
            gui.screenshot().save(path)
            return {"path": path.relative_to(workspace).as_posix(), "screen_size": list(gui.size())}
        if action == "focus":
            window = next(
                (item for item in gui.getAllWindows() if item._hWnd == arguments.get("window_id")), None
            )
            if window is None:
                raise ValueError("Window not found; use observe")
            if window.isMinimized:
                window.restore()
            if not window.isActive:
                ctypes.windll.user32.SetForegroundWindow(ctypes.c_void_p(window._hWnd))
            if not window.isActive:
                raise RuntimeError("Windows did not grant focus to the selected window")
        elif action in {"move", "click"}:
            x, y = arguments.get("x"), arguments.get("y")
            if x is None or y is None or not gui.onScreen(x, y):
                raise ValueError("Provide x and y inside the primary screen")
            gui.moveTo(x, y)
            if action == "click":
                gui.click()
        elif action == "press":
            keys = [key.lower() for key in arguments.get("keys", [])]
            if not keys or any(key not in gui.KEYBOARD_KEYS for key in keys):
                raise ValueError("Provide supported key names")
            held = []
            try:
                for key in keys:
                    gui.keyDown(key, _pause=False)
                    held.append(key)
            finally:
                for key in reversed(held):
                    gui.keyUp(key, _pause=False)
        elif action == "type":
            gui.write(arguments["text"], interval=0)
        return {"action": action, "completed": True}
