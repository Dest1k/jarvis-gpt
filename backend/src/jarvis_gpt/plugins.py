"""Experimental in-process plugin registry (disabled dynamic import by default)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Plugin:
    name: str
    version: str
    tools: dict[str, Any] = field(default_factory=dict)
    description: str = ""


class PluginManager:
    def __init__(self, *, allow_dynamic_import: bool = False) -> None:
        self.plugins: dict[str, Plugin] = {}
        self.allow_dynamic_import = allow_dynamic_import

    def load_plugin(self, path: str) -> dict[str, Any]:
        if not self.allow_dynamic_import:
            return {
                "ok": False,
                "error": "Dynamic plugin import is disabled for safety.",
                "path": path,
            }
        import importlib

        mod = importlib.import_module(path)
        plugin = Plugin(
            name=str(getattr(mod, "NAME", path)),
            version=str(getattr(mod, "VERSION", "0.1")),
            tools=getattr(mod, "get_tools", lambda: {})(),
            description=str(getattr(mod, "DESCRIPTION", "")),
        )
        self.plugins[plugin.name] = plugin
        return {"ok": True, "name": plugin.name, "version": plugin.version}

    def list_plugins(self) -> list[str]:
        return sorted(self.plugins)


def get_plugin_tools() -> dict[str, Any]:
    manager = PluginManager(allow_dynamic_import=False)
    return {
        "plugins.list": manager.list_plugins,
        "plugins.load": manager.load_plugin,
    }
