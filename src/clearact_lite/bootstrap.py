from clearact_lite.application import Application
from clearact_lite.core.ports import ModelProvider
from clearact_lite.integrations.browser import BrowserManager
from clearact_lite.integrations.mcp import MCPManager
from clearact_lite.providers.openai_compatible import OpenAICompatible
from clearact_lite.runtime.delegation import DelegationManager
from clearact_lite.settings import Settings
from clearact_lite.storage.snapshots import FileSnapshots
from clearact_lite.storage.sqlite import SQLiteStore
from clearact_lite.tools.browser import ActBrowser, OpenBrowser, ReadBrowser
from clearact_lite.tools.documents import ReadDocument
from clearact_lite.tools.files import DeliverFile, EditFile, ListFiles, ReadFile, WriteFile
from clearact_lite.tools.history import ReadHistory
from clearact_lite.tools.mcp import CallMCPTool, ListMCPTools
from clearact_lite.tools.registry import ToolRegistry
from clearact_lite.tools.tasks import DelegateTask, PlanTask, SaveCheckpoint


def build_application(
    settings: Settings,
    provider: ModelProvider | None = None,
    mcp_servers: dict | None = None,
) -> Application:
    root = settings.workspace.resolve()
    snapshots = FileSnapshots(root)
    browser, mcp = BrowserManager(root, snapshots), MCPManager(root, mcp_servers)
    delegation = DelegationManager()
    application = Application(
        settings,
        provider or OpenAICompatible(settings),
        SQLiteStore(root / ".clearact" / "history.sqlite3"),
        snapshots,
        ToolRegistry(
            [
                ReadFile(),
                WriteFile(),
                EditFile(),
                PlanTask(delegation),
                DelegateTask(delegation),
                SaveCheckpoint(),
                ReadHistory(),
                DeliverFile(),
                ListFiles(),
                ReadDocument(),
                OpenBrowser(browser),
                ReadBrowser(browser),
                ActBrowser(browser),
                ListMCPTools(mcp),
                CallMCPTool(mcp),
            ],
            [browser, mcp],
        ),
        browser=browser,
        mcp=mcp,
    )
    delegation.bind(application.runner)
    return application
