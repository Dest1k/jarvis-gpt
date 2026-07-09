"""Operator persona: a durable, structured profile the agent reads every turn.

The persona is the "who I am" layer that turns JARVIS from a stateless chat
wrapper into a continuation of the operator. Instead of patching each use case
with a bespoke heuristic (a weather-location cache, a shopping matcher, ...),
the persona captures the operator once — home location, languages, tech stack,
interests, standing "always/never" rules, personal glossary and current focus —
and lets both the LLM and the intent router consult it broadly.

The module intentionally exposes plain functions (``load_persona``,
``render_system_block``, ``home_location`` ...) so the agent runtime can read
the persona straight from storage the same way it already reads
``experience.preferences``. ``PersonaManager`` wraps the same normalization with
audit logging for the API surface.
"""

from __future__ import annotations

from typing import Any

from .config import JarvisSettings
from .storage import JarvisStorage

PERSONA_KEY = "experience.persona"

# List fields share a common shape: deduplicated, trimmed, length-capped.
_LIST_FIELDS: tuple[str, ...] = (
    "languages",
    "expertise",
    "tech_stack",
    "interests",
    "current_focus",
    "standing_instructions",
)

_TEXT_FIELDS: dict[str, int] = {
    "display_name": 80,
    "headline": 240,
    "role": 160,
    "location": 120,
    "timezone": 64,
    "notes": 800,
}

DEFAULT_PERSONA: dict[str, Any] = {
    # Identity
    "display_name": "",
    "headline": "",
    "role": "Системный администратор / технарь",
    "location": "",
    "timezone": "",
    "languages": ["ru"],
    # Competence and context
    "expertise": [],
    "tech_stack": [],
    "interests": [],
    "current_focus": [],
    # Durable directives and shorthand
    "standing_instructions": [],
    "glossary": {},
    "notes": "",
}

# Per-field caps so a persona can stay rich without ever bloating the prompt.
_LIST_LIMITS: dict[str, int] = {
    "languages": 5,
    "expertise": 16,
    "tech_stack": 24,
    "interests": 16,
    "current_focus": 10,
    "standing_instructions": 16,
}

_GLOSSARY_TERM_LIMIT = 40
_GLOSSARY_MEANING_LIMIT = 200
_GLOSSARY_ENTRY_LIMIT = 24

# Fields the router/agent may append single insights to (learned from chat).
INSIGHT_FIELDS: frozenset[str] = frozenset(_LIST_FIELDS)


def load_persona(storage: JarvisStorage) -> dict[str, Any]:
    """Return the normalized persona merged over the defaults."""

    stored = storage.get_runtime_value(PERSONA_KEY, {})
    return normalize_persona(stored if isinstance(stored, dict) else {})


def normalize_persona(value: dict[str, Any]) -> dict[str, Any]:
    merged = {**DEFAULT_PERSONA, **(value if isinstance(value, dict) else {})}
    persona: dict[str, Any] = {}
    for field, limit in _TEXT_FIELDS.items():
        persona[field] = _text(merged.get(field), limit)
    for field in _LIST_FIELDS:
        persona[field] = _string_list(merged.get(field), limit=_LIST_LIMITS[field])
    if not persona["languages"]:
        persona["languages"] = list(DEFAULT_PERSONA["languages"])
    persona["glossary"] = _glossary(merged.get("glossary"))
    return persona


def is_configured(persona: dict[str, Any]) -> bool:
    """True when the operator has supplied something beyond the defaults."""

    for field in ("display_name", "headline", "location", "timezone", "notes"):
        if persona.get(field):
            return True
    if str(persona.get("role") or "") != DEFAULT_PERSONA["role"]:
        return True
    if persona.get("glossary"):
        return True
    for field in _LIST_FIELDS:
        if field == "languages":
            if list(persona.get(field) or []) != DEFAULT_PERSONA["languages"]:
                return True
            continue
        if persona.get(field):
            return True
    return False


def home_location(persona: dict[str, Any]) -> str | None:
    """The operator's home place, usable for weather/local/geo defaults."""

    location = str(persona.get("location") or "").strip()
    return location or None


def primary_language(persona: dict[str, Any]) -> str:
    languages = persona.get("languages") or DEFAULT_PERSONA["languages"]
    return str(languages[0]) if languages else "ru"


def render_system_block(
    persona: dict[str, Any],
    *,
    settings: JarvisSettings | None = None,
    preferences: dict[str, Any] | None = None,
) -> str:
    """Render a compact, high-signal system-prompt block for the persona.

    Only non-empty fields are emitted so the block stays small when the
    operator has not filled things in yet. Returns ``""`` when nothing but the
    defaults are present and no display name can be resolved.
    """

    preferences = preferences if isinstance(preferences, dict) else {}
    display_name = persona.get("display_name") or preferences.get("operator_name") or ""
    if not is_configured(persona) and not display_name:
        return ""
    lines: list[str] = ["Operator persona (who you are working for — treat as durable truth):"]
    if display_name:
        lines.append(f"- name: {display_name}")
    if persona.get("role"):
        lines.append(f"- role: {persona['role']}")
    if persona.get("headline"):
        lines.append(f"- about: {persona['headline']}")
    if persona.get("location"):
        lines.append(f"- home_location: {persona['location']}")
    if persona.get("timezone"):
        lines.append(f"- timezone: {persona['timezone']}")
    languages = persona.get("languages") or []
    if languages:
        lines.append(f"- languages: {', '.join(languages)}")
    _append_list_line(lines, "expertise", persona.get("expertise"))
    _append_list_line(lines, "tech_stack", persona.get("tech_stack"))
    _append_list_line(lines, "interests", persona.get("interests"))
    _append_list_line(lines, "current_focus", persona.get("current_focus"))
    glossary = persona.get("glossary") or {}
    if glossary:
        entries = "; ".join(f"{term} = {meaning}" for term, meaning in glossary.items())
        lines.append(f"- glossary: {entries}")
    if persona.get("notes"):
        lines.append(f"- notes: {persona['notes']}")
    instructions = persona.get("standing_instructions") or []
    if instructions:
        lines.append("Standing operator instructions (honor them unless the operator overrides):")
        lines.extend(f"- {item}" for item in instructions)

    guidance = _guidance(persona, has_name=bool(display_name))
    if guidance:
        lines.append(guidance)

    if len(lines) <= 1 and not guidance:
        return ""
    return "\n".join(lines)


def _guidance(persona: dict[str, Any], *, has_name: bool) -> str:
    parts: list[str] = []
    location = home_location(persona)
    if location:
        parts.append(
            "When a request depends on a place and none is given "
            f"(weather, nearby options, local time, travel), assume {location} "
            "unless the operator names another."
        )
    if persona.get("tech_stack") or persona.get("expertise"):
        parts.append(
            "Bias admin/technical answers toward the operator's known stack and skill level "
            "instead of generic tutorials."
        )
    if persona.get("interests"):
        parts.append(
            "You may lean on the operator's interests to make research and suggestions personal."
        )
    if not has_name and not persona.get("role"):
        return ""
    return " ".join(parts)


class PersonaManager:
    """Audit-logged read/write facade over the operator persona."""

    def __init__(self, *, settings: JarvisSettings, storage: JarvisStorage) -> None:
        self.settings = settings
        self.storage = storage

    def persona(self) -> dict[str, Any]:
        return load_persona(self.storage)

    def update(self, patch: dict[str, Any]) -> dict[str, Any]:
        current = self.persona()
        allowed = {key: value for key, value in patch.items() if key in DEFAULT_PERSONA}
        updated = normalize_persona({**current, **allowed})
        if updated == current:
            return updated
        self.storage.set_runtime_value(PERSONA_KEY, updated)
        self.storage.record_audit(
            actor="operator",
            action="persona.update",
            target_type="runtime",
            target_id=PERSONA_KEY,
            summary="Operator persona updated",
            before=current,
            after=updated,
        )
        self.storage.add_event(
            kind="persona.update",
            title="Operator persona updated",
            payload={"fields": sorted(allowed.keys())},
        )
        return updated

    def add_insight(self, field: str, value: str, *, actor: str = "agent") -> dict[str, Any]:
        """Append one learned fact to a list field (idempotent, capped)."""

        if field not in INSIGHT_FIELDS:
            raise ValueError(f"Persona field {field!r} does not accept insights.")
        text = str(value or "").strip()
        if not text:
            return self.persona()
        current = self.persona()
        existing = list(current.get(field) or [])
        if any(text.lower() == str(item).lower() for item in existing):
            return current
        updated = normalize_persona({**current, field: [*existing, text]})
        if updated == current:
            return updated
        self.storage.set_runtime_value(PERSONA_KEY, updated)
        self.storage.record_audit(
            actor=actor,
            action="persona.insight",
            target_type="runtime",
            target_id=PERSONA_KEY,
            summary=f"Persona {field} learned: {text[:120]}",
            after={"field": field, "value": text},
        )
        return updated

    def system_block(self, preferences: dict[str, Any] | None = None) -> str:
        return render_system_block(
            self.persona(),
            settings=self.settings,
            preferences=preferences,
        )


def _text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _string_list(value: Any, *, limit: int) -> list[str]:
    if isinstance(value, str):
        value = [part for part in value.replace("\n", ",").split(",")]
    if not isinstance(value, list):
        return []
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = " ".join(str(item).split())[:160]
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(text)
        if len(cleaned) >= limit:
            break
    return cleaned


def _glossary(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    glossary: dict[str, str] = {}
    for term, meaning in value.items():
        clean_term = " ".join(str(term).split())[:_GLOSSARY_TERM_LIMIT]
        clean_meaning = " ".join(str(meaning).split())[:_GLOSSARY_MEANING_LIMIT]
        if not clean_term or not clean_meaning:
            continue
        glossary[clean_term] = clean_meaning
        if len(glossary) >= _GLOSSARY_ENTRY_LIMIT:
            break
    return glossary


def _append_list_line(lines: list[str], label: str, value: Any) -> None:
    items = value if isinstance(value, list) else []
    if items:
        lines.append(f"- {label}: {', '.join(str(item) for item in items)}")
