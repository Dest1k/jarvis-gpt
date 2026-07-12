#!/usr/bin/env python3
"""
Plugin System - Improved
"""

import importlib
from dataclasses import dataclass
from typing import Dict, Any, Callable, List


@dataclass
class Plugin:
    name: str
    version: str
    tools: Dict[str, Callable]
    description: str


class PluginManager:
    def __init__(self):
        self.plugins: Dict[str, Plugin] = {}

    def load_plugin(self, module_path: str) -> Plugin:
        mod = importlib.import_module(module_path)
        plugin = Plugin(
            name=getattr(mod, "PLUGIN_NAME", module_path),
            version=getattr(mod, "PLUGIN_VERSION", "0.1"),
            tools=getattr(mod, "get_tools", lambda: {})(),
            description=getattr(mod, "PLUGIN_DESCRIPTION", "")
        )
        self.plugins[plugin.name] = plugin
        return plugin

    def list_plugins(self) -> List[str]:
        return list(self.plugins.keys())


def get_plugin_tools():
    pm = PluginManager()
    return {
        "plugins.list": pm.list_plugins,
        "plugins.load": pm.load_plugin,
    }

print("[plugins.py] Improved.")