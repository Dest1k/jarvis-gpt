from __future__ import annotations

import asyncio

import pytest
from jarvis_gpt.web_orchestrator import (
    WebBudgetExceeded,
    WebMode,
    WebOrchestrator,
    analyze_shopping_source,
    normalize_web_mode,
)


def test_web_modes_are_explicit_and_deadlines_are_hard_capped():
    assert normalize_web_mode("fast-fact", default=WebMode.DEEP_RESEARCH) is WebMode.FAST_FACT
    assert normalize_web_mode("deep", default=WebMode.FAST_FACT) is WebMode.DEEP_RESEARCH
    assert (
        normalize_web_mode("shopping", default=WebMode.FAST_FACT)
        is WebMode.AGGRESSIVE_SHOPPING
    )
    with pytest.raises(ValueError, match="Unsupported web mode"):
        normalize_web_mode("unbounded", default=WebMode.DEEP_RESEARCH)

    orchestrator = WebOrchestrator.create(
        query="fact",
        mode=WebMode.FAST_FACT,
        deadline_sec=999,
    )
    assert orchestrator.limits.deadline_sec == 5.0
    assert orchestrator.metadata()["mode"] == "FAST_FACT"


def test_shared_orchestrator_enforces_global_parallelism_and_operation_budget():
    async def scenario():
        orchestrator = WebOrchestrator.create(query="parallel", mode=WebMode.DEEP_RESEARCH)
        active = 0
        maximum_active = 0

        async def worker(value: int) -> int:
            async def io() -> int:
                nonlocal active, maximum_active
                active += 1
                maximum_active = max(maximum_active, active)
                await asyncio.sleep(0.01)
                active -= 1
                return value * 2

            return await orchestrator.run("fetches", io)

        results = await orchestrator.bounded_map(list(range(8)), worker, concurrency=8)
        assert [item.value for item in results] == [value * 2 for value in range(8)]
        assert 1 < maximum_active <= orchestrator.limits.concurrency

        await orchestrator.budget.reserve(
            "network_bytes", orchestrator.limits.network_bytes
        )
        with pytest.raises(WebBudgetExceeded, match="network_bytes"):
            await orchestrator.budget.reserve("network_bytes", 1)

    asyncio.run(scenario())


def test_shopping_analysis_filters_paid_seo_and_extracts_real_technical_negatives():
    orchestrator = WebOrchestrator.create(
        query="best laptop",
        mode=WebMode.AGGRESSIVE_SHOPPING,
    )
    sources = [
        {
            "url": "https://affiliate.example/top-10?utm_source=ads&aff=42",
            "title": "Top 10 best laptops 2026",
            "excerpt": (
                "Sponsored ultimate buying guide. Exclusive discount coupon. "
                "Click here to buy our number one choice."
            ),
            "fetched": True,
        },
        {
            "url": "https://reddit.com/r/laptops/comments/real-owner",
            "title": "Long-term owner review",
            "excerpt": (
                "After six months the battery drains overnight, which is a serious problem. "
                "The laptop regularly overheats under sustained load and reaches unsafe "
                "thermal temperatures."
            ),
            "fetched": True,
        },
        {
            "url": "https://forums.example.net/thread/thermal",
            "title": "Owner review after firmware update",
            "excerpt": (
                "The machine overheats during video export and the fan noise is a major "
                "drawback even after the latest firmware update."
            ),
            "fetched": True,
        },
        {
            "url": "https://shop.example.com/product/laptop-x",
            "title": "Laptop X $1,299.99",
            "excerpt": "In stock for $1,299.99 with a two year warranty.",
            "fetched": True,
        },
    ]

    accepted, filtered, summary = orchestrator.enrich_shopping_sources(sources)

    assert len(accepted) == 3
    assert filtered[0]["domain"] == "affiliate.example"
    assert filtered[0]["reasons"] == ["sponsored_seo"]
    assert summary["offers"][0]["amount"] == "1299.99"
    assert summary["offers"][0]["currency"] == "USD"
    categories = {
        item["category"] for item in summary["negative_technical_reviews"]
    }
    assert {"battery", "thermal"}.issubset(categories)
    assert summary["corroborated_technical_issues"] == [
        {
            "category": "thermal",
            "domains": ["forums.example.net", "reddit.com"],
            "source_count": 2,
        }
    ]


def test_shopping_analysis_rejects_unfetched_snippet_without_price_or_review_signal():
    analysis = analyze_shopping_source(
        {
            "url": "https://shop.example/catalog/widget",
            "title": "Widget catalog",
            "snippet": "Browse the widget category.",
            "fetched": False,
        }
    )

    assert analysis["excluded"] is True
    assert analysis["exclusion_reasons"] == ["low_signal_snippet"]
