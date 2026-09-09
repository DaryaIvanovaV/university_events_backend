"""
Демонстрация извлечения событий.

Берёт пример сообщения из статьи, прогоняет пре-фильтр и экстрактор,
печатает результат. Пытается обратиться к реальному Ollama; если он
недоступен — использует встроенный фейковый chat_fn, чтобы демо работало
без запущенной модели.

Режим --ollama печатает КАЖДУЮ фазу (проверка сервера → проверка модели →
прогрев → извлечение) с таймингами и heartbeat'ом, чтобы долгий cold start
не выглядел зависанием (первый вызов на холодной модели идёт дольше — это
нормально, прерывать Ctrl+C не нужно).

Запуск:
    python -m scripts.demo_extract              # авто: Ollama или фейк
    python -m scripts.demo_extract --ollama     # только реальный Ollama
"""

from __future__ import annotations

import datetime as dt
import sys
import time

from app.config import settings
from app.llm import ollama_status
from app.llm.extractor import EventExtractor, make_ollama_chat_fn
from app.models.schemas import EventList
from app.services.prefilter import prefilter
from scripts._console import Heartbeat, force_utf8

force_utf8()

SAMPLE = (
    "Увага! Завтра о 14:30 в 405 аудиторії відбудеться гостьова лекція "
    "від ІТ-компанії про розробку на Kotlin. Явка для 4 курсу обов'язкова!"
)
REFERENCE_DATE = dt.date(2026, 4, 2)

# Фейковый ответ — что должна вернуть модель на SAMPLE.
_FAKE = (
    '{"events":[{"event_title":"Гостьова лекція: розробка на Kotlin",'
    '"date":"2026-04-03","time":"14:30","location":"Ауд. 405",'
    '"organizer":"ІТ-компанія","target_audience":"4 курс",'
    '"event_type":"lecture","description":null,"language":"uk","link":null}]}'
)


def _fake_chat_fn(messages, schema):
    return _FAKE


def _run_ollama() -> EventList | None:
    """
    Реальный путь через Ollama с проверками и печатью прогресса.

    Возвращает EventList при успехе или None при обработанной ошибке
    (совет по устранению уже напечатан).
    """
    base = settings.ollama_base_url
    model = settings.ollama_model

    # 1. Сервер поднят?
    print(f"[1/4] Проверяю сервер Ollama на {base} …", flush=True)
    try:
        version = ollama_status.check_server()
    except Exception as exc:
        print(f"  ✗ Сервер недоступен ({type(exc).__name__}).")
        print("    Запустите приложение Ollama (значок в трее) или проверьте:")
        print(f"      Invoke-RestMethod {base}/api/version")
        return None
    print(f"  ✓ Сервер работает (версия {version}).")

    # 2. Модель скачана?
    print(f"[2/4] Проверяю, загружена ли модель {model} …", flush=True)
    try:
        have = ollama_status.model_available(model)
    except Exception as exc:
        print(f"  ✗ Не удалось получить список моделей ({type(exc).__name__}).")
        return None
    if not have:
        print(f"  ✗ Модель {model} не скачана. Выполните:")
        print(f"      ollama pull {model}")
        print("    Если команда 'ollama' не найдена в этом окне PowerShell —")
        print("    откройте НОВОЕ окно (PATH обновляется) или обновите PATH сейчас:")
        print(
            "      $env:Path = "
            "[Environment]::GetEnvironmentVariable('Path','Machine') + ';' + "
            "[Environment]::GetEnvironmentVariable('Path','User')"
        )
        return None
    print(f"  ✓ Модель {model} доступна.")

    # 3. Прогрев: загрузка модели в память (самая долгая фаза на холодную).
    print(
        "[3/4] Загружаю модель в память (первый раз может занять несколько "
        "минут на CPU — НЕ прерывайте Ctrl+C) …",
        flush=True,
    )
    try:
        load_s = ollama_status.warmup(model)
    except Exception as exc:
        print(f"  ✗ Прогрев не удался ({type(exc).__name__}: {exc}).")
        return None
    print(f"  ✓ Модель загружена за {load_s:.1f} с.")

    # 4. Извлечение (модель уже в памяти — обычно секунды).
    print("[4/4] Извлекаю событие из сообщения …", flush=True)
    extractor = EventExtractor(make_ollama_chat_fn())
    start = time.perf_counter()
    try:
        with Heartbeat():
            result = extractor.extract(SAMPLE, REFERENCE_DATE)
    except Exception as exc:
        print(f"  ✗ Извлечение не удалось ({type(exc).__name__}: {exc}).")
        return None
    print(f"  ✓ Готово за {time.perf_counter() - start:.1f} с.")
    return result


def main() -> None:
    force_ollama = "--ollama" in sys.argv

    print("=" * 70)
    print("Сообщение:")
    print(" ", SAMPLE)
    print(f"Дата-ориентир: {REFERENCE_DATE}")
    print("=" * 70)

    pf = prefilter(SAMPLE)
    print(f"\n[Пре-фильтр] кандидат={pf.is_candidate} score={pf.score} "
          f"совпадения={pf.matched}")
    if not pf.is_candidate:
        print("Отсечено пре-фильтром — LLM не вызывается.")
        return

    print()
    result = _run_ollama()
    using_real = result is not None

    if result is None:
        if force_ollama:
            # Явно просили реальный Ollama — не подменяем фейком, выходим.
            sys.exit(1)
        print(
            "\n[LLM] Ollama недоступен — использую встроенный фейк "
            "для демонстрации формата."
        )
        result = EventExtractor(_fake_chat_fn).extract(SAMPLE, REFERENCE_DATE)

    print(f"\n[LLM] источник: {'реальный Ollama' if using_real else 'фейк'}")
    print(f"[LLM] извлечено событий: {len(result.events)}\n")
    for ev in result.events:
        print(ev.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
