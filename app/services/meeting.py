"""
Нормализация данных онлайн-встречи: URL, код доступа, место.

Зачем отдельный шаг, если модель и так извлекает поля: на реальном прогоне
сообщение с «Підключення через Zoom, ідентифікатор конференції 845 2371 9004,
код доступу 316742» дало link=None и пустой description — ID и код пропали
бесследно. Модель хорошо видит URL (8 из 8 на датасете), но код доступа для
неё «не поле». Детерминированный разбор регулярками надёжнее, чем ещё одна
просьба в промте.

Как раскладываются данные (совпадает с conferenceData.entryPoints в Google
Calendar и со свойством URL в iCal):

  link          ровно один абсолютный http(s) URL подключения/регистрации.
                Клиент делает его кликабельным — там не должно быть ничего,
                кроме адреса.
  meeting_code  ID конференции и/или код доступа: "ID 845 2371 9004 · код 316742".
                Код — не URL: в поле ссылки он не кликается и ломает сравнение
                адресов.
  location      "Онлайн (Zoom)" либо физическая аудитория. Приложение по этому
                полю показывает место, URL здесь дублировал бы link.
  description   только проза. Коды и ссылки в прозе приходится парсить заново.

Zoom-ссылки со встроенным ?pwd= остаются как есть (это часть канонического
URL), но код дополнительно кладётся в meeting_code.
"""

from __future__ import annotations

import re

from app.models.schemas import ExtractedEvent

# Абсолютный URL. Хвостовая пунктуация предложения не должна попадать в ссылку.
_URL_RE = re.compile(r"https?://[^\s<>\"'()\[\],]+", re.IGNORECASE)
_BARE_URL_RE = re.compile(r"\bwww\.[^\s<>\"'()\[\],]+", re.IGNORECASE)
_TRAILING = ".,;:!?)»\"'"

# «ідентифікатор конференції 845 2371 9004», «meeting id: 845 2371 9004»
_MEETING_ID_RE = re.compile(
    r"(?:ідентифікатор(?:\s+конференції)?|идентификатор(?:\s+конференции)?"
    r"|meeting\s*id|conference\s*id|номер\s+конференції)"
    r"\s*[:—–-]?\s*([\d][\d\s-]{6,20}\d)",
    re.IGNORECASE,
)
# «код доступу 316742», «пароль: 316742», «passcode 316742»
_PASSCODE_RE = re.compile(
    r"(?:код(?:\s+доступу|\s+доступа)?|пароль|passcode|password|pin)"
    r"\s*[:—–-]?\s*([A-Za-z0-9]{4,16})\b",
    re.IGNORECASE,
)

# Признаки онлайн-формата — для подписи места.
_PLATFORMS = (
    ("zoom", "Zoom"),
    ("google meet", "Google Meet"),
    ("meet.google", "Google Meet"),
    ("microsoft teams", "Microsoft Teams"),
    ("teams", "Microsoft Teams"),
    ("webex", "Webex"),
    ("skype", "Skype"),
    ("bigbluebutton", "BigBlueButton"),
)
_ONLINE_RE = re.compile(r"\b(онлайн|online|дистанційно|дистанционно)\b", re.IGNORECASE)


def _clean_url(url: str) -> str:
    return url.rstrip(_TRAILING)


def find_urls(text: str) -> list[str]:
    """Все ссылки текста, без хвостовой пунктуации предложения."""
    urls = [_clean_url(m.group(0)) for m in _URL_RE.finditer(text)]
    urls += [
        "https://" + _clean_url(m.group(0))
        for m in _BARE_URL_RE.finditer(text)
        if _clean_url(m.group(0)) not in " ".join(urls)
    ]
    return urls


def find_meeting_code(text: str) -> str | None:
    """
    ID конференции и/или код доступа из текста.

    Возвращает нормализованную строку: "ID 845 2371 9004 · код 316742".
    Порядок фиксирован, чтобы одинаковые данные давали одинаковую строку.
    """
    parts: list[str] = []

    id_match = _MEETING_ID_RE.search(text)
    if id_match:
        # Внутренние пробелы Zoom-идентификатора значимы для читаемости.
        raw = re.sub(r"\s+", " ", id_match.group(1)).strip(" -")
        parts.append(f"ID {raw}")

    code_match = _PASSCODE_RE.search(text)
    if code_match:
        parts.append(f"код {code_match.group(1)}")

    return " · ".join(parts) or None


def detect_platform(text: str) -> str | None:
    """Название платформы онлайн-встречи, если она названа в тексте."""
    lower = text.casefold()
    for needle, label in _PLATFORMS:
        if needle in lower:
            return label
    return None


def normalize_meeting(event: ExtractedEvent, source_text: str) -> ExtractedEvent:
    """
    Раскладывает ссылку, код и место по своим полям.

    Ничего не выдумывает: URL и код берутся ТОЛЬКО из исходного текста.
    Значения, которые модель уже нашла, не перетираются — дополняются.
    Идемпотентна: повторный вызов на своём же результате ничего не меняет.
    """
    changes: dict = {}
    urls = find_urls(source_text)

    # 1. link — только настоящий URL из текста.
    #
    # Выдуманный адрес опаснее отсутствующего: студент кликнет и попадёт не
    # туда. Модель реально это делает — на прогоне она подставила ссылку из
    # few-shot примера в сообщение, где никакого URL не было. Поэтому правило
    # жёсткое: адреса нет в исходном тексте → поле очищается.
    link = (event.link or "").strip()
    if link:
        cleaned = _clean_url(link)
        if any(cleaned.rstrip("/") in u or u in cleaned for u in urls):
            if cleaned != link:
                changes["link"] = cleaned
        elif urls:
            changes["link"] = urls[0]  # адрес в тексте есть, но модель дала другой
        else:
            changes["link"] = None  # в тексте ссылок нет вовсе — выдумка
    elif urls:
        changes["link"] = urls[0]

    # 2. meeting_code — из текста, если модель его не увидела.
    if not (event.meeting_code or "").strip():
        code = find_meeting_code(source_text)
        if code:
            changes["meeting_code"] = code

    # 3. location — платформа вместо голого URL, чтобы не дублировать link.
    location = (event.location or "").strip()
    platform = detect_platform(source_text)
    if location and _URL_RE.search(location):
        changes["location"] = f"Онлайн ({platform})" if platform else "Онлайн"
    elif location.casefold() in {"онлайн", "online"} and platform:
        changes["location"] = f"Онлайн ({platform})"
    elif not location and platform and _ONLINE_RE.search(source_text):
        changes["location"] = f"Онлайн ({platform})"

    return event.model_copy(update=changes) if changes else event
