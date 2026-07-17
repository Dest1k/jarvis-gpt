"""The gitignored backend/.env.local secrets loader.

The backend reads secrets only from the process environment. load_local_env_file
lets the owner persist keys (e.g. the Yandex Search API key + folder id) in a
file instead of exporting them on every launch, without ever overriding an
explicit shell export.
"""

from __future__ import annotations

from jarvis_gpt.config import load_local_env_file


def test_loads_key_value_pairs(tmp_path, monkeypatch):
    monkeypatch.delenv("JARVIS_YANDEX_SEARCH_API_KEY", raising=False)
    monkeypatch.delenv("JARVIS_YANDEX_SEARCH_FOLDER_ID", raising=False)
    env_file = tmp_path / ".env.local"
    env_file.write_text(
        "\n".join(
            [
                "# Yandex Search API (AI Studio key)",
                "",
                "JARVIS_YANDEX_SEARCH_API_KEY=AQVN-secret",
                'JARVIS_YANDEX_SEARCH_FOLDER_ID="b1gfolder"',
                "export JARVIS_EXTRA='quoted-value'",
            ]
        ),
        encoding="utf-8",
    )
    applied = load_local_env_file(env_file)
    assert set(applied) == {
        "JARVIS_YANDEX_SEARCH_API_KEY",
        "JARVIS_YANDEX_SEARCH_FOLDER_ID",
        "JARVIS_EXTRA",
    }
    import os

    assert os.environ["JARVIS_YANDEX_SEARCH_API_KEY"] == "AQVN-secret"
    # Surrounding quotes are stripped from both double- and single-quoted values.
    assert os.environ["JARVIS_YANDEX_SEARCH_FOLDER_ID"] == "b1gfolder"
    assert os.environ["JARVIS_EXTRA"] == "quoted-value"


def test_never_overrides_existing_env(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_YANDEX_SEARCH_API_KEY", "from-shell")
    env_file = tmp_path / ".env.local"
    env_file.write_text("JARVIS_YANDEX_SEARCH_API_KEY=from-file", encoding="utf-8")
    applied = load_local_env_file(env_file)
    # Explicit shell export wins; the file value is ignored and not reported.
    assert applied == []

    import os

    assert os.environ["JARVIS_YANDEX_SEARCH_API_KEY"] == "from-shell"


def test_missing_file_is_a_noop(tmp_path):
    assert load_local_env_file(tmp_path / "does-not-exist.env") == []


def test_ignores_malformed_lines(tmp_path, monkeypatch):
    monkeypatch.delenv("JARVIS_OK_KEY", raising=False)
    env_file = tmp_path / ".env.local"
    env_file.write_text(
        "\n".join(["no-equals-sign", "   ", "=novalue", "JARVIS_OK_KEY=ok"]),
        encoding="utf-8",
    )
    assert load_local_env_file(env_file) == ["JARVIS_OK_KEY"]


def test_env_file_override_via_env_var(tmp_path, monkeypatch):
    monkeypatch.delenv("JARVIS_FROM_OVERRIDE", raising=False)
    env_file = tmp_path / "custom.env"
    env_file.write_text("JARVIS_FROM_OVERRIDE=yes", encoding="utf-8")
    monkeypatch.setenv("JARVIS_ENV_FILE", str(env_file))
    assert load_local_env_file() == ["JARVIS_FROM_OVERRIDE"]
