"""Reliable live smoke for the feature package. Exit 1 on any FAIL.

Usage (backend up, token in D:\\jarvis\\.jarvis\\api.token):
  uv run python scripts/live_feature_smoke.py
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

TOKEN_PATH = Path(r"D:\jarvis\.jarvis\api.token")
BASE = "http://127.0.0.1:8000"
LLM = "http://127.0.0.1:8001/v1/models"
FILES = Path(r"D:\jarvis\data\jarvis-gpt\files")

PASS = 0
FAIL = 0
ROWS: list[str] = []


def _token() -> str:
    return TOKEN_PATH.read_text(encoding="utf-8").strip()


def api(method: str, path: str, body: dict | None = None, headers: dict | None = None, timeout: float = 120.0):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    h = {
        "Authorization": f"Bearer {_token()}",
        "Content-Type": "application/json; charset=utf-8",
    }
    if headers:
        h.update(headers)
    req = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if not raw:
                return resp.status, None
            return resp.status, json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"raw": raw}
        return exc.code, payload


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        ROWS.append(f"PASS  {name}" + (f" — {detail}" if detail else ""))
    else:
        FAIL += 1
        ROWS.append(f"FAIL  {name}" + (f" — {detail}" if detail else ""))


def approve_and_retry(method: str, path: str, body: dict) -> tuple[int, object]:
    status, payload = api(method, path, body)
    if status != 428:
        return status, payload
    detail = (payload or {}).get("detail") or {}
    if not isinstance(detail, dict):
        return status, payload
    apr = detail.get("approval_id")
    if not apr:
        return status, payload
    api("PATCH", f"/api/approvals/{apr}", {"status": "approved"})
    return api(method, path, body, headers={"x-jarvis-approval-id": apr})


def main() -> int:
    # 0) LLM + status
    try:
        with urllib.request.urlopen(LLM, timeout=10) as resp:
            models = json.loads(resp.read().decode("utf-8"))
        check("llm.models", bool(models.get("data")), str([m.get("id") for m in models.get("data", [])]))
    except Exception as exc:  # noqa: BLE001
        check("llm.models", False, str(exc))

    st, status = api("GET", "/api/status")
    check("status.http", st == 200, f"status={st}")
    if st == 200 and isinstance(status, dict):
        check("status.notices_field", "notices" in status)
        check("status.service_mode_field", "service_mode" in status)
        profile = ((status.get("settings") or {}).get("profile") or {}).get("name")
        check("status.profile_qwen36", profile == "qwen36-vl", f"profile={profile}")

    # 1) service mode on → chat short-circuit → off
    until = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + 600))
    st, sm = approve_and_retry(
        "PUT",
        "/api/admin/runtime/service-mode",
        {"enabled": True, "message": "smoke service mode", "until": until},
    )
    check("service_mode.enable", st == 200 and isinstance(sm, dict) and (sm.get("service_mode") or {}).get("enabled") is True, f"st={st}")

    st, status_sm = api("GET", "/api/status")
    notices_list = (status_sm or {}).get("notices") if isinstance(status_sm, dict) else None
    if not isinstance(notices_list, list):
        notices_list = [notices_list] if notices_list else []
    check(
        "service_mode.notices",
        st == 200 and any(isinstance(n, dict) and n.get("kind") == "service_mode" for n in notices_list),
        f"st={st} notices={notices_list!r}",
    )

    t0 = time.time()
    st, chat = api("POST", "/api/chat", {"message": "ping while service mode"})
    dt = time.time() - t0
    ans = (chat or {}).get("answer") if isinstance(chat, dict) else ""
    check(
        "service_mode.chat_short_circuit",
        st == 200 and isinstance(ans, str) and "smoke service mode" in ans and dt < 5.0,
        f"st={st} dt={dt:.2f}s ans={ans[:80]!r}",
    )

    st, sm_off = approve_and_retry(
        "PUT",
        "/api/admin/runtime/service-mode",
        {"enabled": False, "message": "", "until": None},
    )
    check("service_mode.disable", st == 200 and (sm_off.get("service_mode") or {}).get("enabled") is False, f"st={st}")

    # 2) live model
    t0 = time.time()
    st, chat = api("POST", "/api/chat", {"message": "Ответь ровно одним словом: PONG. Без пояснений."}, timeout=240)
    dt = time.time() - t0
    ans = str((chat or {}).get("answer") or "")
    check("live.model_pong", st == 200 and "PONG" in ans.upper(), f"st={st} dt={dt:.2f}s ans={ans[:120]!r}")
    cid = (chat or {}).get("conversation_id")
    mid = (chat or {}).get("message_id")
    check("live.message_ids", bool(cid) and bool(mid), f"cid={cid} mid={mid}")

    if cid:
        st, msgs = api("GET", f"/api/conversations/{cid}/messages")
        mlist = msgs if isinstance(msgs, list) else (msgs or {}).get("messages") or []
        has_ts = all(isinstance(m.get("created_at"), str) and "T" in m["created_at"] for m in mlist)
        check("timestamps.messages", st == 200 and len(mlist) >= 2 and has_ts, f"n={len(mlist)}")

    # 3) layout / typo live paths
    st, chat = api("POST", "/api/chat", {"message": "ыефегы системы одной фразой"}, timeout=240)
    ans = str((chat or {}).get("answer") or "")
    check("typo.layout_status", st == 200 and len(ans) > 10, f"ans={ans[:100]!r}")

    st, chat = api(
        "POST",
        "/api/chat",
        {"message": "авыавыфвафвафвафваф  кратко температура и загрузка GPU через nvidia-smi"},
        timeout=240,
    )
    ans = str((chat or {}).get("answer") or "").lower()
    ok_gpu = st == 200 and (
        "gpu" in ans
        or "5090" in ans
        or "nvidia" in ans
        or "vram" in ans
        or "видео" in ans
        or "°c" in ans
        or ("%" in ans and any(x in ans for x in ("56", "50", "60", "загруз", "temp")))
    )
    check("typo.face_smash_gpu", ok_gpu, f"ans={ans[:140]!r}")

    # 4) memory vault / graph data
    st, vault = api("GET", "/api/memory/vault", timeout=90)
    if st == 200 and isinstance(vault, dict):
        nodes = vault.get("nodes") or []
        edges = vault.get("edges") or []
        check("memory.vault_graph", len(nodes) > 0 and len(edges) > 0, f"nodes={len(nodes)} edges={len(edges)}")
    else:
        check("memory.vault_graph", False, f"st={st}")

    # 5) IAM RU descriptions + full preset rights
    st, ids = api("GET", "/api/admin/security-ids")
    if st == 200 and isinstance(ids, list):
        ru = sum(1 for x in ids if re.search(r"[\u0400-\u04FF]", x.get("description") or ""))
        check("iam.security_ids_ru_majority", ru >= 100, f"ru={ru}/{len(ids)}")
        # Admin/core should be RU
        core = [x for x in ids if x.get("security_id", "").startswith("admin.")]
        core_ru = sum(1 for x in core if re.search(r"[\u0400-\u04FF]", x.get("description") or ""))
        check("iam.admin_ids_all_ru", core_ru == len(core) and len(core) > 0, f"ru={core_ru}/{len(core)}")
    else:
        check("iam.security_ids_ru_majority", False, f"st={st}")

    st, presets = api("GET", "/api/admin/presets")
    if st == 200 and isinstance(presets, list):
        guest = next((p for p in presets if p.get("preset_key") == "guest"), None)
        owner = next((p for p in presets if p.get("preset_key") == "owner"), None)
        check(
            "iam.preset_guest_full_list",
            bool(guest and isinstance(guest.get("security_ids"), list) and len(guest["security_ids"]) >= 5),
            f"n={len((guest or {}).get('security_ids') or [])}",
        )
        check(
            "iam.preset_owner_full_list",
            bool(owner and len(owner.get("security_ids") or []) > 50),
            f"n={len((owner or {}).get('security_ids') or [])}",
        )
    else:
        check("iam.preset_guest_full_list", False, f"st={st}")
        check("iam.preset_owner_full_list", False, f"st={st}")

    # 6) users create (local + tg pre-provision)
    st, user = approve_and_retry(
        "POST",
        "/api/admin/users",
        {
            "kind": "local",
            "display_name": f"smoke_local_{int(time.time())}",
            "preset_key": "guest",
            "reason": "live smoke",
        },
    )
    check(
        "users.create_local_guest",
        st in (200, 201) and (user or {}).get("preset_key") == "guest",
        f"st={st} {user}",
    )

    tg_id = 900000000 + (int(time.time()) % 1000000)
    st, tg = approve_and_retry(
        "POST",
        "/api/admin/users",
        {
            "kind": "telegram",
            "display_name": "smoke_tg",
            "preset_key": "guest",
            "telegram_user_id": tg_id,
            "username": f"smoke_tg_{tg_id}",
            "reason": "live smoke tg",
        },
    )
    check(
        "users.create_tg_preprovision",
        st in (200, 201) and str((tg or {}).get("provider_subject_id") or "") == str(tg_id),
        f"st={st} tg={tg}",
    )
    pre = Path(r"D:\jarvis\data\jarvis-gpt\state\telegram_pre_provisioned.json")
    if pre.exists():
        data = json.loads(pre.read_text(encoding="utf-8"))
        check("users.tg_preprovision_file", tg_id in (data.get("chat_ids") or []) or str(tg_id) in map(str, data.get("chat_ids") or []))
    else:
        check("users.tg_preprovision_file", False, "missing file")

    # 7) archives
    from jarvis_gpt.archive_runtime import (  # noqa: WPS433
        ArchivePasswordError,
        ArchiveUnsupportedError,
        list_archive,
        read_archive_member,
    )

    plain = FILES / "live_smoke_secret.zip"
    pwd_zip = FILES / "live_smoke_pwd.zip"
    aes_zip = FILES / "live_smoke_aes.zip"

    if plain.exists():
        listing = list_archive(plain)
        check("archive.plain_list", listing.get("ok") is True and listing.get("member_count", 0) >= 1)
    else:
        check("archive.plain_list", False, "fixture missing")

    if pwd_zip.exists():
        try:
            list_archive(pwd_zip)
            check("archive.zip_requires_password", False, "expected password error")
        except ArchivePasswordError:
            check("archive.zip_requires_password", True)
        except Exception as exc:  # noqa: BLE001
            check("archive.zip_requires_password", False, repr(exc))

        listing = list_archive(pwd_zip, password="secret42")
        check("archive.zip_list_with_password", listing.get("ok") is True)

        payload = read_archive_member(pwd_zip, "_secret.txt", password="secret42")
        check(
            "archive.zip_read_with_password",
            payload.get("ok") is True and "pwd_payload_99" in str(payload.get("text_preview") or ""),
            str(payload.get("text_preview")),
        )
        try:
            read_archive_member(pwd_zip, "_secret.txt", password="wrong")
            check("archive.zip_wrong_password", False, "expected error")
        except ArchivePasswordError:
            check("archive.zip_wrong_password", True)
        except Exception as exc:  # noqa: BLE001
            check("archive.zip_wrong_password", False, repr(exc))
    else:
        for name in (
            "archive.zip_requires_password",
            "archive.zip_list_with_password",
            "archive.zip_read_with_password",
            "archive.zip_wrong_password",
        ):
            check(name, False, "fixture missing")

    if aes_zip.exists():
        try:
            read_archive_member(aes_zip, "_secret_aes.txt", password="secret42")
            check("archive.aes_clear_error", False, "expected unsupported")
        except ArchiveUnsupportedError as exc:
            check("archive.aes_clear_error", "AES" in str(exc) or "неподдерж" in str(exc).lower(), str(exc)[:160])
        except Exception as exc:  # noqa: BLE001
            # Before restart may still be cryptic; mark fail so we know.
            check("archive.aes_clear_error", False, f"{type(exc).__name__}: {exc}")
    else:
        check("archive.aes_clear_error", False, "aes fixture missing")

    # 8) frontend graph marker
    try:
        with urllib.request.urlopen("http://127.0.0.1:3000/", timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        check("frontend.home_200", resp.status == 200)
        # static export embeds chunk refs; accept MemoryGraph or Граф
        check(
            "frontend.graph_bundle",
            ("MemoryGraph" in html) or ("graph" in html.lower()) or ("Граф" in html),
            f"len={len(html)}",
        )
    except Exception as exc:  # noqa: BLE001
        check("frontend.home_200", False, str(exc))
        check("frontend.graph_bundle", False, str(exc))

    # 9) operator_text unit-level (in-process)
    from jarvis_gpt.operator_text import normalize_operator_message, try_layout_flip

    check("operator.layout_open", try_layout_flip("jnrhjq файл") == "открой файл")
    check("operator.layout_status", "status" in normalize_operator_message("ыефегы"))
    check("operator.no_destroy_en", try_layout_flip("open calculator") == "open calculator")

    print("\n".join(ROWS))
    print(f"\nTOTAL  pass={PASS} fail={FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
