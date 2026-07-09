"""Result integrity layer: verify answers against the task and ship deliverables.

The project theses promise "understand the task, then deliver a finished
result" — and neither half is solved by model scale alone. A local model
happily returns a confident answer that skips half of what was asked, and a
finished mission leaves only a trail of step notes instead of an
operator-facing result. This module owns the missing "definition of done":

- ``build_verification_messages`` + ``parse_verdict``: one budgeted critic pass
  that checks a draft answer against the operator's task and the task kernel's
  completion criteria, returning a strict JSON verdict.
- repair prompts: one bounded repair round. Request/response chat may rewrite
  the whole answer; an already-streamed answer can only receive a short
  correction addendum (streamed text cannot be retracted); a mission step
  rewrites its report.
- mission report: a deterministic compilation of a finished mission's steps
  plus an optional LLM synthesis over it, so a completed mission ends with a
  deliverable, not just a progress bar.

Every reader degrades safely: unparseable critic output means "pass" (a broken
verifier must never damage a good answer), and the mission report always keeps
the deterministic fallback.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

VERIFICATION_PROMPT = (
    "answer-verification-v1\n"
    "Ты слой самопроверки JARVIS. Тебе дают задачу оператора, критерии готовности и "
    "черновой ответ. Оцени ТОЛЬКО соответствие ответа задаче, а не стиль.\n"
    "Верни РОВНО один JSON без markdown: "
    '{"verdict": "pass" | "revise", "score": 0.0-1.0, '
    '"missing": ["чего не хватает", ...], "fix_hint": "как исправить"}.\n'
    "revise — только если ответ пропускает или искажает явную часть задачи, "
    "противоречит приведённым observation/фактам, выдаёт непроверенное за факт "
    "или игнорирует критерии готовности. Стилистика, длина и тон — это pass.\n"
    "Если ответ честно называет ограничение (нет данных, нужен approval, LLM/инструмент "
    "недоступен) — это pass, а не revise.\n"
    "Не придумывай новые требования, которых нет в задаче."
)

REPAIR_REWRITE_PROMPT = (
    "Самопроверка нашла пробелы в твоём черновике (см. ниже). Перепиши ответ оператору "
    "ЦЕЛИКОМ, закрыв перечисленные пробелы, сохранив всё верное из черновика и не "
    "выдумывая фактов, которых нет в контексте и observation. Верни только готовый "
    "ответ по-русски, без JSON и без упоминания самопроверки."
)

REPAIR_ADDENDUM_PROMPT = (
    "Ответ уже показан оператору, его нельзя переписать. Самопроверка нашла пробелы "
    "(см. ниже). Верни КОРОТКОЕ дополнение по-русски, начинающееся со строки "
    "'Поправка после самопроверки:' — только недостающие или исправленные пункты, "
    "без повтора уже сказанного, без JSON и без извинений."
)

MISSION_REPORT_PROMPT = (
    "mission-report-v1\n"
    "Ты формируешь итоговый отчёт завершённой миссии JARVIS для оператора. Используй "
    "только цель миссии и фактические результаты шагов ниже — ничего не выдумывай. "
    "Структура: короткий вывод (что в итоге сделано и какой результат у оператора на "
    "руках), затем ключевые подтверждённые факты по шагам, затем что осталось или "
    "рекомендуется дальше. Пиши по-русски, кратко и по делу, без JSON и markdown-заголовков."
)


@dataclass(frozen=True)
class Verdict:
    verdict: str
    score: float
    missing: tuple[str, ...] = ()
    fix_hint: str = ""

    def payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "verdict": self.verdict,
            "score": round(self.score, 3),
        }
        if self.missing:
            payload["missing"] = list(self.missing)
        if self.fix_hint:
            payload["fix_hint"] = self.fix_hint
        return payload


def build_verification_messages(
    *,
    task: str,
    answer: str,
    criteria: Sequence[str] = (),
    observations: Sequence[str] = (),
    kind: str = "chat",
) -> list[dict[str, str]]:
    criteria_block = "\n".join(f"- {item}" for item in list(criteria)[:6]) or "- нет явных"
    observation_block = "\n".join(f"- {item[:400]}" for item in list(observations)[:6])
    user_parts = [
        f"kind: {kind}",
        f"Задача оператора:\n{task[:2400]}",
        f"Критерии готовности:\n{criteria_block}",
    ]
    if observation_block:
        user_parts.append(f"Факты из инструментов (observation):\n{observation_block}")
    user_parts.append(f"Черновой ответ:\n{answer[:6000]}")
    return [
        {"role": "system", "content": VERIFICATION_PROMPT},
        {"role": "user", "content": "\n\n".join(user_parts)},
    ]


def parse_verdict(content: str) -> Verdict | None:
    """Parse the critic's JSON verdict; any deviation means None (treated as pass)."""

    text = str(content or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if match:
        text = match.group(0)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    verdict = str(data.get("verdict") or "").strip().lower()
    if verdict not in {"pass", "revise"}:
        return None
    try:
        score = float(data.get("score") or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    raw_missing = data.get("missing")
    missing: tuple[str, ...] = ()
    if isinstance(raw_missing, list):
        missing = tuple(
            " ".join(str(item).split())[:200] for item in raw_missing[:6] if str(item).strip()
        )
    return Verdict(
        verdict=verdict,
        score=max(0.0, min(1.0, score)),
        missing=missing,
        fix_hint=" ".join(str(data.get("fix_hint") or "").split())[:400],
    )


def build_repair_messages(
    base_messages: list[dict[str, str]],
    draft: str,
    verdict: Verdict,
    *,
    mode: str = "rewrite",
) -> list[dict[str, str]]:
    """One bounded repair round: rewrite the draft or produce a short addendum."""

    prompt = REPAIR_REWRITE_PROMPT if mode == "rewrite" else REPAIR_ADDENDUM_PROMPT
    gaps = "\n".join(f"- {item}" for item in verdict.missing) or "- см. подсказку"
    critique = f"{prompt}\n\nПробелы:\n{gaps}"
    if verdict.fix_hint:
        critique += f"\nПодсказка: {verdict.fix_hint}"
    return [
        *base_messages,
        {"role": "assistant", "content": draft},
        {"role": "system", "content": critique},
    ]


def deterministic_mission_report(mission: dict[str, Any]) -> str:
    """Offline-safe mission report compiled from persisted step results."""

    tasks = mission.get("tasks") if isinstance(mission.get("tasks"), list) else []
    done = [task for task in tasks if task.get("status") == "done"]
    lines = [
        f"Итог миссии «{mission.get('title', '')}»",
        f"Цель: {mission.get('goal', '')}",
        f"Выполнено шагов: {len(done)} из {len(tasks)}.",
    ]
    if tasks:
        lines.append("Шаги:")
        for task in tasks:
            note = " ".join(str(task.get("notes") or "").split())
            suffix = f" — {note[:220]}" if note else ""
            lines.append(f"- [{task.get('status')}] {task.get('title')}{suffix}")
    return "\n".join(lines)


def build_mission_report_messages(mission: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": MISSION_REPORT_PROMPT},
        {
            "role": "user",
            "content": (
                "Синтезируй итоговый отчёт миссии для оператора.\n\n"
                + deterministic_mission_report(mission)[:6000]
            ),
        },
    ]


def valid_mission_report(text: str) -> bool:
    """Reject empty or router/tool-JSON-shaped synthesis output."""

    cleaned = str(text or "").strip()
    if len(cleaned) < 40:
        return False
    if cleaned.startswith(("{", "[")):
        return False
    lowered = cleaned.lower()
    return not ('"tool"' in lowered or '"route"' in lowered or '"verdict"' in lowered)
