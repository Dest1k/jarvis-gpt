"""Optional registration helpers for experimental ideal-branch modules.

Production document tools are registered directly in ``tools.ToolRegistry``.
This module only exposes diagnostics / optional experimental callables and must
never print or raise on import.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

LOGGER = logging.getLogger("jarvis.ideal_tools")

ToolMap = dict[str, Callable[..., Any]]


def _safe_import_tools(loader: Callable[[], ToolMap], label: str) -> ToolMap:
    try:
        tools = loader()
        return tools if isinstance(tools, dict) else {}
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("Ideal module %s unavailable: %s", label, exc)
        return {}


def get_all_ideal_tools() -> ToolMap:
    tools: ToolMap = {}
    try:
        from .document_agent import get_document_agent_tools

        tools.update(_safe_import_tools(get_document_agent_tools, "document_agent"))
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("document_agent import failed: %s", exc)
    try:
        from .vision import get_vision_tools

        tools.update(_safe_import_tools(get_vision_tools, "vision"))
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("vision import failed: %s", exc)
    try:
        from .calendar_integration import get_calendar_tools

        tools.update(_safe_import_tools(get_calendar_tools, "calendar"))
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("calendar import failed: %s", exc)
    try:
        from .email_integration import get_email_tools

        tools.update(_safe_import_tools(get_email_tools, "email"))
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("email import failed: %s", exc)
    try:
        from .voice import get_voice_tools

        tools.update(_safe_import_tools(get_voice_tools, "voice"))
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("voice import failed: %s", exc)
    try:
        from .knowledge_graph import get_knowledge_graph_tools

        tools.update(_safe_import_tools(get_knowledge_graph_tools, "knowledge_graph"))
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("knowledge_graph import failed: %s", exc)
    try:
        from .plugins import get_plugin_tools

        tools.update(_safe_import_tools(get_plugin_tools, "plugins"))
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("plugins import failed: %s", exc)
    try:
        from .proactive_briefing import get_briefing_tools

        tools.update(_safe_import_tools(get_briefing_tools, "proactive_briefing"))
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("proactive_briefing import failed: %s", exc)
    return tools


def register_all_ideal_tools(registry: Any) -> dict[str, Any]:
    """Best-effort registration into a registry that supports ``register_many`` or ``add``.

    Document production tools should already be present via ``ToolRegistry``.
    This helper is for experimental modules only.
    """

    tools = get_all_ideal_tools()
    registered: list[str] = []
    skipped: list[str] = []
    if hasattr(registry, "register_many") and callable(registry.register_many):
        try:
            registry.register_many(tools)
            registered = list(tools)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("register_many failed: %s", exc)
            skipped = list(tools)
    else:
        for name, handler in tools.items():
            try:
                if hasattr(registry, "add") and callable(registry.add):
                    # ToolRegistry expects ToolSpec; skip incompatible shapes.
                    skipped.append(name)
                    continue
                if hasattr(registry, "register") and callable(registry.register):
                    registry.register(name, handler)
                    registered.append(name)
                else:
                    skipped.append(name)
            except Exception:  # noqa: BLE001
                skipped.append(name)
    return {
        "registered": registered,
        "skipped": skipped,
        "available": sorted(tools),
        "note": (
            "Production document tools live in ToolRegistry "
            "(documents.* via document_surfer). Ideal modules are experimental."
        ),
    }
