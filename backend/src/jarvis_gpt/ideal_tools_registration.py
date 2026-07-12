#!/usr/bin/env python3
"""
Ideal Tools Registration - Final large push
"""

try:
    from jarvis_gpt.vision import get_vision_tools
    from jarvis_gpt.document_agent import get_document_agent_tools
    from jarvis_gpt.calendar_integration import get_calendar_tools
    from jarvis_gpt.email_integration import get_email_tools
    from jarvis_gpt.voice import get_voice_tools
    from jarvis_gpt.knowledge_graph import get_knowledge_graph_tools
    from jarvis_gpt.plugins import get_plugin_tools
    from jarvis_gpt.proactive_briefing import get_briefing_tools
except ImportError:
    get_vision_tools = lambda: {}
    get_document_agent_tools = lambda: {}
    get_calendar_tools = lambda: {}
    get_email_tools = lambda: {}
    get_voice_tools = lambda: {}
    get_knowledge_graph_tools = lambda: {}
    get_plugin_tools = lambda: {}
    get_briefing_tools = lambda: {}


def register_all_ideal_tools(registry):
    try:
        registry.register_many(get_vision_tools())
        registry.register_many(get_document_agent_tools())
        registry.register_many(get_calendar_tools())
        registry.register_many(get_email_tools())
        registry.register_many(get_voice_tools())
        registry.register_many(get_knowledge_graph_tools())
        registry.register_many(get_plugin_tools())
        registry.register_many(get_briefing_tools())
        print("All ideal tools registered successfully.")
    except Exception as e:
        print(f"Error: {e}")


def get_all_ideal_tools():
    tools = {}
    tools.update(get_vision_tools())
    tools.update(get_document_agent_tools())
    tools.update(get_calendar_tools())
    tools.update(get_email_tools())
    tools.update(get_voice_tools())
    tools.update(get_knowledge_graph_tools())
    tools.update(get_plugin_tools())
    tools.update(get_briefing_tools())
    return tools

print("[ideal_tools_registration] Final large push.")