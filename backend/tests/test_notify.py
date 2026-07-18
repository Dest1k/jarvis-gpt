from __future__ import annotations

from jarvis_gpt.notify import telegram_targets


def test_single_allowed_chat_is_default_alert_target() -> None:
    token, targets = telegram_targets(
        {"TELEGRAM_BOT_TOKEN": "token", "TELEGRAM_ALLOWED_CHAT_IDS": "42"}
    )

    assert token == "token"
    assert targets == (42,)


def test_multiple_allowed_chats_do_not_implicitly_receive_private_alerts() -> None:
    _, targets = telegram_targets(
        {"TELEGRAM_BOT_TOKEN": "token", "TELEGRAM_ALLOWED_CHAT_IDS": "42, 99"}
    )

    assert targets == ()


def test_alert_targets_are_deduplicated_and_intersected_with_allowlist() -> None:
    _, targets = telegram_targets(
        {
            "TELEGRAM_BOT_TOKEN": "token",
            "TELEGRAM_ALLOWED_CHAT_IDS": "42, 99",
            "TELEGRAM_ALERT_CHAT_IDS": "99, 123, 99",
        }
    )

    assert targets == (99,)


def test_explicit_target_cannot_escape_allowlist() -> None:
    _, targets = telegram_targets(
        {
            "TELEGRAM_BOT_TOKEN": "token",
            "TELEGRAM_ALLOWED_CHAT_IDS": "42, 99",
            "TELEGRAM_ALERT_CHAT_IDS": "42",
        },
        requested_chat_ids=(99, 123),
    )

    assert targets == (99,)
