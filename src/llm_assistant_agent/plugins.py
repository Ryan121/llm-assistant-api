"""Plugin system for custom tools.

Allows extending the agent with custom tools without modifying core code.
Plugins are discovered from:
- ``~/.assist/plugins/`` - user plugins
- ``.assist/plugins/`` in workspace - project-specific plugins
- Paths in ``ASSIST_PLUGINS`` environment variable

Each plugin is a Python module exposing:
- ``TOOL_SCHEMAS``: list of tool schema dicts (same format as core tools)
- ``ToolBox`` subclass or individual tool methods
- Optional ``setup()`` and ``teardown()`` lifecycle hooks

Example plugin::

    TOOL_SCHEMAS = [{
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "Fetch content from a URL",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to fetch"},
                },
                "required": ["url"],
            },
        },
    }]

    class PluginToolBox:
        def fetch_url(self, arguments):
            import urllib.request
            url = arguments.get("url", "")
            try:
                with urllib.request.urlopen(url, timeout=10) as response:
                    return response.read().decode("utf-8")[:10000]
            except Exception as exc:
                return f"Error fetching {url}: {exc}"
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

__all__ = ["Plugin", "PluginLoader", "PluginToolBox"]


class PluginToolBox(Protocol):
    """Protocol for plugin toolboxes."""

    def invoke(self, name: str, arguments: dict[str, Any]) -> str:
        """Execute a tool call. Returns text for the model."""
        ...


@dataclass
class Plugin:
    """A loaded plugin."""

    name: str
    path: Path
    tool_schemas: list[dict[str, Any]] = field(default_factory=list)
    toolbox_class: type | None = None
    setup_hook: callable | None = None
    teardown_hook: callable | None = None

    def create_toolbox(self) -> Any:
        """Create a toolbox instance for this plugin."""
        if self.toolbox_class:
            return self.toolbox_class()
        return None


class PluginLoader:
    """Discovers and loads plugins."""

    def __init__(self) -> None:
        self.plugins: list[Plugin] = []
        self._loaded_paths: set[Path] = set()

    def discover(self, workspace_root: Path | None = None) -> list[Plugin]:
        """Find all available plugins.

        Searches in order:
        1. Paths from ASSIST_PLUGINS env var (colon-separated)
        2. ~/.assist/plugins/
        3. .assist/plugins/ in workspace (if workspace_root provided)
        """
        self.plugins = []
        self._loaded_paths = set()

        search_paths: list[Path] = []

        # 1. Environment variable paths
        env_paths = os.environ.get("ASSIST_PLUGINS", "")
        if env_paths:
            for path_str in env_paths.split(":"):
                if path_str.strip():
                    search_paths.append(Path(path_str.strip()).expanduser())

        # 2. User plugins directory
        home = Path.home()
        user_plugins = home / ".assist" / "plugins"
        if user_plugins.is_dir():
            search_paths.append(user_plugins)

        # 3. Workspace plugins directory
        if workspace_root:
            workspace_plugins = workspace_root / ".assist" / "plugins"
            if workspace_plugins.is_dir():
                search_paths.append(workspace_plugins)

        # Load plugins from each search path
        for search_path in search_paths:
            self._load_from_directory(search_path)

        return self.plugins

    def _load_from_directory(self, directory: Path) -> None:
        """Load all plugins from a directory."""
        if not directory.is_dir():
            return

        for path in directory.glob("*.py"):
            if path.name.startswith("_"):
                continue
            if path in self._loaded_paths:
                continue

            plugin = self._load_plugin(path)
            if plugin:
                self.plugins.append(plugin)
                self._loaded_paths.add(path)

    def _load_plugin(self, path: Path) -> Plugin | None:
        """Load a single plugin module."""
        try:
            spec = importlib.util.spec_from_file_location(path.stem, path)
            if spec is None or spec.loader is None:
                return None

            module = importlib.util.module_from_spec(spec)
            sys.modules[path.stem] = module
            spec.loader.exec_module(module)

            plugin = Plugin(
                name=path.stem,
                path=path,
                tool_schemas=getattr(module, "TOOL_SCHEMAS", []),
                toolbox_class=getattr(module, "PluginToolBox", None),
                setup_hook=getattr(module, "setup", None),
                teardown_hook=getattr(module, "teardown", None),
            )

            # Call setup hook if present
            if plugin.setup_hook:
                with contextlib.suppress(Exception):
                    plugin.setup_hook()

            return plugin

        except Exception:
            # Silently skip broken plugins
            return None

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """Collect all tool schemas from loaded plugins."""
        schemas = []
        for plugin in self.plugins:
            schemas.extend(plugin.tool_schemas)
        return schemas

    def create_plugin_toolbox(self) -> PluginToolBox | None:
        """Create a combined toolbox for all plugins."""
        if not self.plugins:
            return None

        class CombinedPluginToolBox:
            def __init__(self, plugins: list[Plugin]) -> None:
                self.instances = []
                for plugin in plugins:
                    instance = plugin.create_toolbox()
                    if instance:
                        self.instances.append(instance)

            def invoke(self, name: str, arguments: dict[str, Any]) -> str:
                for instance in self.instances:
                    if hasattr(instance, name):
                        method = getattr(instance, name)
                        try:
                            return method(arguments)
                        except Exception as exc:
                            return f"Error in {name}: {exc}"
                return f"Unknown plugin tool: {name}"

        return CombinedPluginToolBox(self.plugins)

    def teardown(self) -> None:
        """Call teardown hooks for all plugins."""
        for plugin in self.plugins:
            if plugin.teardown_hook:
                with contextlib.suppress(Exception):
                    plugin.teardown_hook()
