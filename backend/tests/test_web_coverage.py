"""Coverage for the internet-surfing expansion: feeds, archive, weather, watches."""

from __future__ import annotations

import asyncio
import ipaddress
import json

import httpx
from jarvis_gpt.agent import AgentRuntime
from jarvis_gpt.autonomy_executor import AutonomyExecutor
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.event_bus import EventBus
from jarvis_gpt.experience import ExperienceManager
from jarvis_gpt.learning import LearningEngine
from jarvis_gpt.llm import LLMRouter
from jarvis_gpt.operations import OperationsManager
from jarvis_gpt.storage import JarvisStorage
from jarvis_gpt.tools import ToolRegistry, _parse_feed_entries

RSS_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Server News</title>
    <item>
      <title>Proxmox 9 released</title>
      <link>https://example.com/proxmox-9</link>
      <pubDate>Thu, 09 Jul 2026 10:00:00 GMT</pubDate>
      <description>&lt;p&gt;Major &lt;b&gt;update&lt;/b&gt; is out.&lt;/p&gt;</description>
    </item>
    <item>
      <title>Debian point release</title>
      <link>https://example.com/debian</link>
      <pubDate>Wed, 08 Jul 2026 10:00:00 GMT</pubDate>
      <description>Security fixes.</description>
    </item>
  </channel>
</rss>
"""

ATOM_SAMPLE = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Release feed</title>
  <entry>
    <title>v2.1.0</title>
    <link rel="alternate" href="https://example.com/releases/v2.1.0"/>
    <updated>2026-07-09T12:00:00Z</updated>
    <summary>Bug fixes and speedups.</summary>
  </entry>
</feed>
"""


def _runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    return settings, storage


def _allow_public(monkeypatch):
    monkeypatch.setattr(
        "jarvis_gpt.tools._public_resolved_addresses",
        lambda _hostname: [ipaddress.ip_address("93.184.216.34")],
    )


def test_parse_feed_entries_rss_and_atom():
    rss_title, rss_entries = _parse_feed_entries(RSS_SAMPLE, limit=10)
    assert rss_title == "Server News"
    assert len(rss_entries) == 2
    assert rss_entries[0]["title"] == "Proxmox 9 released"
    assert rss_entries[0]["link"] == "https://example.com/proxmox-9"
    assert "Major update is out." in rss_entries[0]["summary"]

    atom_title, atom_entries = _parse_feed_entries(ATOM_SAMPLE, limit=10)
    assert atom_title == "Release feed"
    assert atom_entries[0]["link"] == "https://example.com/releases/v2.1.0"
    assert atom_entries[0]["published"].startswith("2026-07-09")


def test_parse_feed_entries_rejects_garbage():
    import pytest

    with pytest.raises(ValueError):
        _parse_feed_entries("<html><body>not a feed</body></html>", limit=5)
    with pytest.raises(ValueError):
        _parse_feed_entries("plain text", limit=5)
    with pytest.raises(ValueError, match="DTD and entity"):
        _parse_feed_entries(
            '<!DOCTYPE rss [<!ENTITY x "expanded">]><rss><channel>&x;</channel></rss>',
            limit=5,
        )


def test_web_feed_tool_returns_entries_and_evidence(monkeypatch, tmp_path):
    class FakeResponse:
        status_code = 200
        headers = {"content-type": "application/rss+xml"}

        async def aiter_bytes(self):
            yield RSS_SAMPLE.encode("utf-8")

    class FakeStream:
        async def __aenter__(self):
            return FakeResponse()

        async def __aexit__(self, *_args):
            return None

    class FakeClient:
        def __init__(self, *args, **kwargs):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def stream(self, method, url, *, headers, follow_redirects):
            assert "rss" in headers["Accept"]
            return FakeStream()

    _allow_public(monkeypatch)
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    settings, storage = _runtime(monkeypatch, tmp_path)
    tools = ToolRegistry(settings, storage, LLMRouter(settings))

    result = asyncio.run(tools.run("web.feed", {"url": "https://example.com/rss.xml"}))

    assert result.ok is True
    assert result.data["feed_title"] == "Server News"
    assert len(result.data["entries"]) == 2
    assert result.data["evidence_id"].startswith("ev")
    storage.close()


def test_web_archive_reads_wayback_snapshot(monkeypatch, tmp_path):
    availability = {
        "archived_snapshots": {
            "closest": {
                "available": True,
                "url": "http://web.archive.org/web/20260101000000/https://example.com/page",
                "timestamp": "20260101000000",
            }
        }
    }

    class FakeGetResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return availability

    class FakeClient:
        def __init__(self, *args, **kwargs):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, *, params, headers):
            assert "archive.org" in url
            assert params["url"] == "https://example.com/page"
            return FakeGetResponse()

    async def fake_fetch(_ctx, args):
        from jarvis_gpt.models import ToolRunResponse

        assert args["url"].startswith("https://web.archive.org/")
        return ToolRunResponse(
            tool="web.fetch",
            ok=True,
            summary="Fetched URL with HTTP 200.",
            data={"url": args["url"], "text": "archived page text", "status_code": 200},
        )

    _allow_public(monkeypatch)
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr("jarvis_gpt.tools._web_fetch", fake_fetch)
    settings, storage = _runtime(monkeypatch, tmp_path)
    tools = ToolRegistry(settings, storage, LLMRouter(settings))

    result = asyncio.run(tools.run("web.archive", {"url": "https://example.com/page"}))

    assert result.ok is True
    assert result.data["snapshot_timestamp"] == "20260101000000"
    assert result.data["text"] == "archived page text"
    assert "historical" in result.data["archive_note"]
    storage.close()


def test_web_archive_reports_missing_snapshot(monkeypatch, tmp_path):
    class FakeGetResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"archived_snapshots": {}}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, *, params, headers):
            return FakeGetResponse()

    _allow_public(monkeypatch)
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    settings, storage = _runtime(monkeypatch, tmp_path)
    tools = ToolRegistry(settings, storage, LLMRouter(settings))

    result = asyncio.run(tools.run("web.archive", {"url": "https://example.com/page"}))

    assert result.ok is False
    assert "No Wayback snapshot" in result.summary
    storage.close()


def test_web_weather_formats_russian_report(monkeypatch, tmp_path):
    geocode_payload = {
        "results": [
            {
                "name": "Казань",
                "admin1": "Татарстан",
                "country": "Россия",
                "latitude": 55.79,
                "longitude": 49.12,
            }
        ]
    }
    forecast_payload = {
        "current": {
            "temperature_2m": 21.4,
            "apparent_temperature": 20.1,
            "relative_humidity_2m": 55,
            "wind_speed_10m": 3.2,
            "weather_code": 2,
        },
        "daily": {
            "time": ["2026-07-10", "2026-07-11"],
            "temperature_2m_min": [15.0, 14.2],
            "temperature_2m_max": [24.0, 22.5],
            "precipitation_probability_max": [10, 60],
            "weather_code": [1, 61],
        },
    }

    class FakeGetResponse:
        def __init__(self, payload):
            self._payload = payload
            self.status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self, *args, **kwargs):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, *, params, headers):
            if "geocoding" in url:
                assert params["name"] == "Казань"
                return FakeGetResponse(geocode_payload)
            assert params["latitude"] == 55.79
            return FakeGetResponse(forecast_payload)

    _allow_public(monkeypatch)
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    settings, storage = _runtime(monkeypatch, tmp_path)
    tools = ToolRegistry(settings, storage, LLMRouter(settings))

    result = asyncio.run(tools.run("web.weather", {"location": "Казань", "days": 2}))

    assert result.ok is True
    report = result.data["report"]
    assert "Казань" in report
    assert "21.4°C" in report
    assert "переменная облачность" in report
    assert "2026-07-11" in report
    assert "небольшой дождь" in report
    assert result.data["source"] == "open-meteo.com"
    storage.close()


def test_weather_route_prefers_open_meteo_tool(monkeypatch, tmp_path):
    settings, storage = _runtime(monkeypatch, tmp_path)
    agent = AgentRuntime(
        settings=settings, storage=storage, llm=LLMRouter(settings), bus=EventBus()
    )
    captured = []

    async def fake_run(name, arguments=None, **kwargs):
        from jarvis_gpt.models import ToolRunResponse

        captured.append(name)
        if name == "web.weather":
            return ToolRunResponse(
                tool="web.weather",
                ok=True,
                summary="Weather resolved for Казань.",
                data={
                    "report": "Погода — Казань: сейчас 21°C, ясно.",
                    "source": "open-meteo.com",
                    "location": "Казань",
                },
            )
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(agent.chat("какая погода в Казани завтра?"))

    assert "web.weather" in captured
    assert "web.search" not in captured
    assert response.answer == "Погода — Казань: сейчас 21°C, ясно."
    storage.close()


def test_web_watch_add_list_remove_and_limit(monkeypatch, tmp_path):
    _allow_public(monkeypatch)
    settings, storage = _runtime(monkeypatch, tmp_path)
    tools = ToolRegistry(settings, storage, LLMRouter(settings))

    added = asyncio.run(
        tools.run(
            "web.watch.add",
            {"url": "https://example.com/price", "label": "цена", "cadence": "15m"},
        )
    )
    assert added.ok is True
    job_id = added.data["job_id"]

    duplicate = asyncio.run(
        tools.run("web.watch.add", {"url": "https://example.com/price", "label": "цена"})
    )
    assert duplicate.ok is True
    assert duplicate.data["existing"] is True
    assert duplicate.data["job_id"] == job_id

    listed = asyncio.run(tools.run("web.watch.list", {}))
    assert listed.ok is True
    assert listed.data["active"] == 1
    assert listed.data["watches"][0]["url"] == "https://example.com/price"

    removed = asyncio.run(tools.run("web.watch.remove", {"job_id": job_id}))
    assert removed.ok is True
    listed_after = asyncio.run(tools.run("web.watch.list", {}))
    assert listed_after.data["active"] == 0

    # The cap refuses watch number 13.
    for index in range(12):
        result = asyncio.run(
            tools.run("web.watch.add", {"url": f"https://example.com/w{index}"})
        )
        assert result.ok is True
    overflow = asyncio.run(tools.run("web.watch.add", {"url": "https://example.com/w99"}))
    assert overflow.ok is False
    assert "limit" in overflow.summary.lower()
    storage.close()


def test_web_watch_job_baseline_then_change(monkeypatch, tmp_path):
    settings, storage = _runtime(monkeypatch, tmp_path)
    llm = LLMRouter(settings)
    agent = AgentRuntime(settings=settings, storage=storage, llm=llm, bus=EventBus())
    operations = OperationsManager(settings=settings, storage=storage)
    executor = AutonomyExecutor(
        settings=settings,
        storage=storage,
        operations=operations,
        agent=agent,
        experience=ExperienceManager(settings=settings, storage=storage),
        llm=llm,
        telemetry=object(),
        dispatcher=object(),
        learning=LearningEngine(storage),
    )
    pages = ["Цена: 45 990 ₽", "Цена: 45 990 ₽", "Цена: 39 990 ₽"]
    calls = {"count": 0}

    async def fake_run(name, arguments=None, **kwargs):
        from jarvis_gpt.models import ToolRunResponse

        assert name == "web.fetch"
        text = pages[min(calls["count"], len(pages) - 1)]
        calls["count"] += 1
        return ToolRunResponse(
            tool="web.fetch",
            ok=True,
            summary="Fetched URL with HTTP 200.",
            data={"url": arguments["url"], "text": text},
        )

    monkeypatch.setattr(agent.tools, "run", fake_run)
    payload = {"url": "https://example.com/gpu", "label": "цена GPU"}

    baseline = asyncio.run(executor.run_kind("web.watch", payload))
    unchanged = asyncio.run(executor.run_kind("web.watch", payload))
    changed = asyncio.run(executor.run_kind("web.watch", payload))

    assert baseline["ok"] is True and baseline["data"]["baseline"] is True
    assert unchanged["data"]["changed"] is False
    assert changed["data"]["changed"] is True
    assert "39 990" in changed["data"]["current"]
    assert "45 990" in changed["data"]["previous"]
    memories = storage.search_memory("watch", limit=5)
    assert any("зафиксировал изменение" in item["content"] for item in memories)
    events = [item for item in storage.list_events(limit=20) if item["kind"] == "web.watch"]
    assert events, "a web.watch change event must be recorded"
    storage.close()


def test_web_watch_job_kind_is_persisted(monkeypatch, tmp_path):
    settings, storage = _runtime(monkeypatch, tmp_path)
    operations = OperationsManager(settings=settings, storage=storage)
    job = operations.create_job(
        {
            "kind": "web.watch",
            "title": "Watch: пример",
            "cadence": "30m",
            "payload": {"url": "https://example.com/x"},
        }
    )
    assert job["kind"] == "web.watch"
    assert job["budget"]["max_runs"] == 500
    stored = json.dumps(operations.list_jobs(), ensure_ascii=False)
    assert "web.watch" in stored
    storage.close()
