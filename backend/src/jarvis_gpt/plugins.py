#!/usr/bin/env python3
"""
Plugin System - Large chunk
"""

import importlib
from dataclasses import dataclass
from typing import Dict, Any, List


@dataclass
class Plugin:
    name: str
    version: str
    tools: Dict[str, Any]
    description: str


class PluginManager:
    def __init__(self):
        self.plugins = {}

    def load_plugin(self, path: str):
        mod = importlib.import_module(path)
        p = Plugin(
            name=getattr(mod, "PLUGIN_NAME", path),
            version=getattr(mod, "VERSION", "0.1"),
            tools=getattr(mod, "get_tools", lambda: {})(),
            description=getattr(mod, "DESCRIPTION", "")
        )
        self.plugins[p.name] = p
        return p

    def list_plugins(self):
        return list(self.plugins.keys())


def get_plugin_tools():
    pm = PluginManager()
    return {
        "plugins.list": pm.list_plugins,
        "plugins.load": pm.load_plugin,
    }

print("[plugins.py] Large chunk.")