"""
Тесты разбора списков в настройках: admins / channels.

Герметично — через Settings(..., _env_file=None), без чтения реального .env
(init-kwargs имеют приоритет над env-переменными, файл отключён). Ключевой кейс —
несколько администраторов (id через запятую) и терпимость к случайным
кавычкам/скобкам вокруг значений.
"""

from app.config import Settings


def _settings(**kwargs) -> Settings:
    return Settings(_env_file=None, **kwargs)


def test_admins_multiple_comma_separated_with_spaces():
    assert _settings(admin_chat_ids="111, 222 ,333").admins == [111, 222, 333]


def test_admins_tolerates_quotes_and_brackets():
    # частая ошибка — кавычки/скобки вокруг id; они не должны ронять админов
    assert _settings(admin_chat_ids="'111', \"222\", [333]").admins == [111, 222, 333]


def test_admins_skips_junk_and_empty():
    assert _settings(admin_chat_ids="111, , abc, 222").admins == [111, 222]
    assert _settings(admin_chat_ids="").admins == []


def test_channels_keeps_at_and_id_prefixes():
    assert _settings(allowed_channels="@cs, -1001234567890").channels == [
        "@cs",
        "-1001234567890",
    ]
