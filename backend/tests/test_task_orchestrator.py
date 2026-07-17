"""Universal multi-step orchestrator: plan -> execute (blackboard) -> synthesize.

The engine is injected with an LLM and a tool runner, so these tests drive the whole
plan/execute/synthesize flow with stubs — no agent, no network, no model.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from jarvis_gpt.task_orchestrator import (
    StepResult,
    TaskOrchestrator,
    _first_http_url,
    _resolve_placeholders,
    _tool_result_text,
    parse_plan,
)


@dataclass
class _LLM:
    ok: bool = True
    content: str = ""


@dataclass
class _Tool:
    ok: bool = True
    summary: str = ""
    data: dict = field(default_factory=dict)


def test_parse_plan_valid_json():
    content = (
        '{"steps":[{"id":"s1","goal":"search","kind":"tool","tool":"web.search",'
        '"arguments":{"query":"x"},"depends_on":[]},'
        '{"id":"s2","goal":"summarize","kind":"reason","depends_on":["s1"]}]}'
    )
    plan = parse_plan(content, "goal", allowed_tools={"web.search"})
    assert [s.id for s in plan.steps] == ["s1", "s2"]
    assert plan.steps[0].kind == "tool" and plan.steps[0].tool == "web.search"
    assert plan.steps[1].kind == "reason" and plan.steps[1].depends_on == ["s1"]


def test_parse_plan_falls_back_to_single_step_on_garbage():
    plan = parse_plan("sorry, I cannot output json", "do the thing", allowed_tools=set())
    assert len(plan.steps) == 1
    assert plan.steps[0].kind == "reason"
    assert plan.steps[0].goal == "do the thing"


def test_parse_plan_unknown_tool_degrades_to_reason():
    content = (
        '{"steps":[{"id":"s1","goal":"g","kind":"tool",'
        '"tool":"does.not.exist","arguments":{}}]}'
    )
    plan = parse_plan(content, "goal", allowed_tools={"web.search"})
    assert plan.steps[0].kind == "reason"
    assert plan.steps[0].tool is None


def test_parse_plan_drops_forward_dependencies():
    content = (
        '{"steps":[{"id":"s1","goal":"a","kind":"reason","depends_on":["s2"]},'
        '{"id":"s2","goal":"b","kind":"reason","depends_on":["s1"]}]}'
    )
    plan = parse_plan(content, "goal", allowed_tools=set())
    assert plan.steps[0].depends_on == []  # forward ref to s2 dropped
    assert plan.steps[1].depends_on == ["s1"]


def test_resolve_placeholders_threads_prior_output():
    bb = {"s1": StepResult(step_id="s1", title="t", ok=True, output="PARIS")}
    resolved = _resolve_placeholders({"query": "weather in {{s1}} today"}, bb)
    assert resolved == {"query": "weather in PARIS today"}


def test_structured_placeholder_pulls_a_field_from_prior_step_data():
    # A step can hand the NEXT tool a specific discovered value (e.g. the cheapest
    # store URL) from a prior step's structured .data, not just the whole summary.
    bb = {
        "s1": StepResult(
            step_id="s1",
            title="t",
            ok=True,
            output="полный текст выдачи",
            data={"top_url": "https://shop/x", "candidates": [{"url": "https://a"}]},
        )
    }
    assert _resolve_placeholders({"url": "{{s1.top_url}}"}, bb) == {"url": "https://shop/x"}
    assert _resolve_placeholders("open {{s1.candidates.0.url}}", bb) == "open https://a"
    # A missing field falls back to the text output rather than blanking the argument.
    assert _resolve_placeholders("{{s1.nope}}", bb) == "полный текст выдачи"
    # Whole-text reference still works unchanged.
    assert _resolve_placeholders("{{s1}}", bb) == "полный текст выдачи"


def test_end_to_end_plan_execute_synthesize():
    plan_json = (
        '{"steps":['
        '{"id":"s1","goal":"найти столицу Франции","kind":"tool","tool":"web.search",'
        '"arguments":{"query":"capital of France"},"depends_on":[]},'
        '{"id":"s2","goal":"погода в {{s1}}","kind":"reason","depends_on":["s1"]}]}'
    )
    seen_reason_context: list[str] = []

    async def complete(messages):
        system = messages[0]["content"]
        user = messages[-1]["content"]
        if "планировщик" in system:
            return _LLM(True, plan_json)
        if "под-задачу" in system:
            seen_reason_context.append(user)
            return _LLM(True, "Погода солнечная, +20")
        if "итоговый ответ" in system:
            return _LLM(True, "Столица — Париж; погода солнечная, +20.")
        return _LLM(False, "")

    tool_calls: list[tuple[str, dict]] = []

    async def run_tool(name, arguments):
        tool_calls.append((name, arguments))
        return _Tool(True, "Париж", {})

    orch = TaskOrchestrator(
        complete=complete,
        run_tool=run_tool,
        tool_specs=[("web.search", "search the web")],
    )
    result = asyncio.run(orch.run("узнай столицу Франции и погоду там"))

    assert result.ok
    assert [s.id for s in result.plan.steps] == ["s1", "s2"]
    # The tool step ran with exactly the planned arguments.
    assert tool_calls == [("web.search", {"query": "capital of France"})]
    # The blackboard carried s1's output into s2's curated context.
    assert any("Париж" in ctx for ctx in seen_reason_context)
    assert result.results[0].output == "Париж"
    assert "Париж" in result.answer


def test_plan_complete_brain_handles_planning_only():
    # Planning may use a stronger brain (frontier) while execution stays local; the
    # dedicated planner is used for the plan, the execution brain for steps + synthesis.
    plan_json = '{"steps":[{"id":"s1","goal":"подумать","kind":"reason"}]}'
    plan_calls: list = []
    exec_calls: list = []

    async def plan_complete(messages):
        plan_calls.append(messages)
        return _LLM(True, plan_json)

    async def complete(messages):
        exec_calls.append(messages)
        return _LLM(True, "результат")

    async def run_tool(name, arguments):  # pragma: no cover - no tool step here
        raise AssertionError("no tool step expected")

    orch = TaskOrchestrator(
        complete=complete,
        run_tool=run_tool,
        tool_specs=[],
        plan_complete=plan_complete,
    )
    result = asyncio.run(orch.run("сделай что-то"))
    assert len(plan_calls) == 1  # exactly one planning call, via the dedicated brain
    assert len(exec_calls) >= 1  # reason step + synthesis went to the execution brain
    assert result.ok


def test_research_backstop_runs_when_plan_produced_no_data():
    # A reason-only plan yields no external data, so the deterministic research
    # backstop fires once on the goal to ground the answer.
    plan_json = '{"steps":[{"id":"s1","goal":"порассуждать","kind":"reason"}]}'
    tool_calls: list = []

    async def complete(messages):
        if "планировщик" in messages[0]["content"]:
            return _LLM(True, plan_json)
        return _LLM(True, "короткий текст")

    async def run_tool(name, arguments):
        tool_calls.append((name, arguments))
        return _Tool(True, "РЕАЛЬНЫЕ ДАННЫЕ " * 12, {})

    orch = TaskOrchestrator(
        complete=complete,
        run_tool=run_tool,
        tool_specs=[("web.research", "research")],
        fallback_query_tool="web.research",
    )
    result = asyncio.run(orch.run("узнай цену X"))
    assert tool_calls == [("web.research", {"query": "узнай цену X", "limit": 5})]
    assert result.ok


def test_no_backstop_when_plan_is_already_grounded():
    plan_json = (
        '{"steps":[{"id":"s1","goal":"найти","kind":"tool","tool":"web.research",'
        '"arguments":{"query":"x"}}]}'
    )
    tool_calls: list = []

    async def complete(messages):
        if "планировщик" in messages[0]["content"]:
            return _LLM(True, plan_json)
        return _LLM(True, "итог")

    async def run_tool(name, arguments):
        tool_calls.append((name, arguments))
        return _Tool(True, "ДАННЫЕ " * 30, {})

    orch = TaskOrchestrator(
        complete=complete,
        run_tool=run_tool,
        tool_specs=[("web.research", "r")],
        fallback_query_tool="web.research",
    )
    asyncio.run(orch.run("цель"))
    assert len(tool_calls) == 1  # only the planned step ran; no backstop needed


def test_first_http_url_extracts_and_trims():
    assert (
        _first_http_url("Лучший вариант: https://dns-shop.ru/p/123 — дёшево")
        == "https://dns-shop.ru/p/123"
    )
    assert _first_http_url("ссылка https://x.com/a).") == "https://x.com/a"
    assert _first_http_url("нет ссылки тут") is None


def test_browser_open_recovers_url_from_the_search_step():
    # The planner hands browser.open prose ("Лучший вариант"); the engine must recover
    # the real URL from the search step it depends on, not fail on a non-URL argument.
    plan_json = (
        '{"steps":['
        '{"id":"s1","goal":"найти","kind":"tool","tool":"web.research","arguments":{"query":"5090"}},'
        '{"id":"s2","goal":"открыть лучший","kind":"tool","tool":"browser.open",'
        '"arguments":{"url":"Лучший вариант"},"depends_on":["s1"]}]}'
    )
    calls: list = []

    async def complete(messages):
        if "планировщик" in messages[0]["content"]:
            return _LLM(True, plan_json)
        return _LLM(True, "итог")

    async def run_tool(name, arguments):
        calls.append((name, dict(arguments)))
        if name == "web.research":
            return _Tool(True, "Дешевле всего: https://dns-shop.ru/product/rtx-5090/ в наличии", {})
        return _Tool(True, "opened", {})

    orch = TaskOrchestrator(
        complete=complete,
        run_tool=run_tool,
        tool_specs=[("web.research", "r"), ("browser.open", "o")],
    )
    asyncio.run(orch.run("открой самый дешёвый 5090"))
    open_call = next(call for call in calls if call[0] == "browser.open")
    assert open_call[1]["url"] == "https://dns-shop.ru/product/rtx-5090/"


def test_browser_open_recovers_url_with_no_url_key():
    # The planner omits the url key entirely; browser.open must still receive a real link
    # recovered from the search step it depends on (the previously-uncovered edge).
    plan_json = (
        '{"steps":['
        '{"id":"s1","goal":"найти","kind":"tool","tool":"web.research","arguments":{"query":"5090"}},'
        '{"id":"s2","goal":"открыть","kind":"tool","tool":"browser.open",'
        '"arguments":{},"depends_on":["s1"]}]}'
    )
    calls: list = []

    async def complete(messages):
        if "планировщик" in messages[0]["content"]:
            return _LLM(True, plan_json)
        return _LLM(True, "итог")

    async def run_tool(name, arguments):
        calls.append((name, dict(arguments)))
        if name == "web.research":
            return _Tool(True, "Самый дешёвый: https://www.regard.ru/product/999/ есть", {})
        return _Tool(True, "opened", {})

    orch = TaskOrchestrator(
        complete=complete,
        run_tool=run_tool,
        tool_specs=[("web.research", "r"), ("browser.open", "o")],
    )
    asyncio.run(orch.run("открой самый дешёвый 5090"))
    open_call = next(call for call in calls if call[0] == "browser.open")
    assert open_call[1]["url"] == "https://www.regard.ru/product/999/"


def test_browser_open_recovers_url_placed_under_another_key():
    # The planner puts the link (with prose) under a non-url key ("link"); it must still
    # be extracted into url so browser.open opens the real page.
    plan_json = (
        '{"steps":['
        '{"id":"s1","goal":"найти","kind":"tool","tool":"web.research","arguments":{"query":"5090"}},'
        '{"id":"s2","goal":"открыть","kind":"tool","tool":"browser.open",'
        '"arguments":{"link":"открой https://shop.example/p/5090 пожалуйста"},'
        '"depends_on":["s1"]}]}'
    )
    calls: list = []

    async def complete(messages):
        if "планировщик" in messages[0]["content"]:
            return _LLM(True, plan_json)
        return _LLM(True, "итог")

    async def run_tool(name, arguments):
        calls.append((name, dict(arguments)))
        if name == "web.research":
            return _Tool(True, "варианты найдены", {})
        return _Tool(True, "opened", {})

    orch = TaskOrchestrator(
        complete=complete,
        run_tool=run_tool,
        tool_specs=[("web.research", "r"), ("browser.open", "o")],
    )
    asyncio.run(orch.run("открой ссылку"))
    open_call = next(call for call in calls if call[0] == "browser.open")
    assert open_call[1]["url"] == "https://shop.example/p/5090"


def test_tool_result_text_surfaces_report_sources_and_prices():
    # The one-line summary alone starved synthesis; the step output must now carry the
    # report body plus a compact source list with prices and URLs.
    text = _tool_result_text(
        "Internet research inspected 2 source(s).",
        {
            "report": "Сравнение цен на RTX 5090 в российских магазинах.",
            "results": [
                {
                    "title": "RTX 5090 — DNS",
                    "url": "https://www.dns-shop.ru/product/rtx-5090/",
                    "price": "199 990 ₽",
                    "snippet": "В наличии, доставка сегодня.",
                },
                {"title": "RTX 5090 — Ozon", "url": "https://ozon.ru/rtx5090"},
            ],
        },
    )
    assert "Сравнение цен на RTX 5090" in text
    assert "199 990 ₽" in text
    assert "https://www.dns-shop.ru/product/rtx-5090/" in text
    assert "https://ozon.ru/rtx5090" in text
    # Bounded so a huge report cannot swamp the synthesis prompt.
    assert len(_tool_result_text("s", {"report": "x" * 9000})) <= 4000


def test_tool_result_text_lists_structured_offers():
    # A store page contributes individual name — price offers, not just a bare link.
    text = _tool_result_text(
        "inspected 1 source",
        {
            "sources": [
                {
                    "title": "RTX 5090 — Яндекс Маркет",
                    "url": "https://market.yandex.ru/search?text=rtx+5090",
                    "price": "477 712 ₽",
                    "products": [
                        {"name": "MSI RTX 5090 Gaming Trio", "price": "539 740 ₽"},
                        {"name": "nVidia RTX 5090 32Gb", "price": "477 712 ₽"},
                    ],
                }
            ]
        },
    )
    assert "MSI RTX 5090 Gaming Trio — 539 740 ₽" in text
    assert "nVidia RTX 5090 32Gb — 477 712 ₽" in text


def test_synthesis_sees_evidence_and_grounding_rules():
    # End-to-end: the tool's data (report + priced source) must reach the synthesis
    # prompt, and the synthesis system prompt must forbid inventing missing facts.
    plan_json = (
        '{"steps":[{"id":"s1","goal":"найти цены","kind":"tool","tool":"web.research",'
        '"arguments":{"query":"rtx 5090"}}]}'
    )
    synth_user: list[str] = []
    synth_system: list[str] = []

    async def complete(messages):
        system = messages[0]["content"]
        if "планировщик" in system:
            return _LLM(True, plan_json)
        if "итоговый ответ" in system:
            synth_system.append(system)
            synth_user.append(messages[-1]["content"])
            return _LLM(True, "ответ по источникам")
        return _LLM(True, "x")

    async def run_tool(name, arguments):
        return _Tool(
            True,
            "Internet research inspected 1 source(s).",
            {
                "results": [
                    {
                        "title": "RTX 5090 DNS",
                        "url": "https://dns-shop.ru/p/5090",
                        "price": "199 990 ₽",
                    }
                ]
            },
        )

    orch = TaskOrchestrator(
        complete=complete, run_tool=run_tool, tool_specs=[("web.research", "r")]
    )
    asyncio.run(orch.run("сравни цены на rtx 5090"))
    assert synth_user, "synthesis was not reached"
    # The real evidence (price + url) is in front of the synthesizer now.
    assert "199 990 ₽" in synth_user[0]
    assert "https://dns-shop.ru/p/5090" in synth_user[0]
    # And it is told not to fabricate what the sources do not contain.
    assert "собственных знаний" in synth_system[0]


def test_failed_planner_still_answers():
    async def complete(messages):
        system = messages[0]["content"]
        if "планировщик" in system:
            return _LLM(False, "")  # planner unavailable
        if "под-задачу" in system:
            return _LLM(True, "Прямой ответ по цели.")
        if "итоговый ответ" in system:
            return _LLM(True, "Готовый ответ.")
        return _LLM(False, "")

    async def run_tool(name, arguments):  # pragma: no cover - no tool step in fallback
        raise AssertionError("no tool step expected")

    orch = TaskOrchestrator(complete=complete, run_tool=run_tool, tool_specs=[])
    result = asyncio.run(orch.run("сделай что-нибудь полезное"))
    assert result.ok
    assert len(result.plan.steps) == 1  # degraded to a single reasoning step
    assert result.answer
