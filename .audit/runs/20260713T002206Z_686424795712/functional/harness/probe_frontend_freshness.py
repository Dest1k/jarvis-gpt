#!/usr/bin/env python3
"""Verify served frontend assets against the local production build/source."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.request import ProxyHandler, Request, build_opener


class Assets(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "script" and values.get("src"):
            self.urls.append(values["src"] or "")
        if tag == "link" and values.get("href") and values.get("rel") in {
            "stylesheet",
            "manifest",
            "icon",
        }:
            self.urls.append(values["href"] or "")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch(opener, url: str) -> dict[str, object]:
    with opener.open(Request(url, headers={"Cache-Control": "no-cache"}), timeout=20) as response:
        data = response.read()
        return {
            "url": url,
            "status": response.status,
            "content_type": response.headers.get("Content-Type"),
            "cache_control": response.headers.get("Cache-Control"),
            "etag": response.headers.get("ETag"),
            "size": len(data),
            "sha256": digest(data),
            "body": data,
        }


def serializable(record: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in record.items() if key != "body"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--base-url", default="http://localhost:3000/")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit(f"refusing to overwrite {args.output}")

    opener = build_opener(ProxyHandler({}))
    root = fetch(opener, args.base_url)
    parser_html = Assets()
    parser_html.feed(root["body"].decode("utf-8", "replace"))
    asset_urls = []
    for raw in parser_html.urls:
        absolute = urljoin(args.base_url, raw)
        if urlparse(absolute).netloc == urlparse(args.base_url).netloc:
            asset_urls.append(absolute)
    unique_assets = list(dict.fromkeys(asset_urls))[:40]
    assets = [fetch(opener, url) for url in unique_assets]
    sw = fetch(opener, urljoin(args.base_url, "/sw.js"))
    manifest = fetch(opener, urljoin(args.base_url, "/manifest.webmanifest"))

    source_sw = (args.repo / "frontend" / "public" / "sw.js").read_bytes()
    source_manifest = (args.repo / "frontend" / "public" / "manifest.webmanifest").read_bytes()
    result = {
        "schema": "jarvis.functional-frontend-freshness.v1",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "base_url": args.base_url,
        "root": serializable(root),
        "assets": [serializable(item) for item in assets],
        "service_worker": {
            **serializable(sw),
            "source_sha256": digest(source_sw),
            "matches_source": digest(sw["body"]) == digest(source_sw),
        },
        "manifest": {
            **serializable(manifest),
            "source_sha256": digest(source_manifest),
            "matches_source": digest(manifest["body"]) == digest(source_manifest),
        },
    }
    result["summary"] = {
        "root_ok": root["status"] == 200,
        "assets_ok": bool(assets) and all(item["status"] == 200 and item["size"] for item in assets),
        "asset_count": len(assets),
        "service_worker_matches": result["service_worker"]["matches_source"],
        "manifest_matches": result["manifest"]["matches_source"],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["summary"], ensure_ascii=False))
    return 0 if all(value for key, value in result["summary"].items() if key != "asset_count") else 1


if __name__ == "__main__":
    raise SystemExit(main())
