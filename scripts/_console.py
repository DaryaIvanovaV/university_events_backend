"""
Мелкие консольные утилиты для скриптов.

Вынесены из demo_extract.py, чтобы check_prompt.py не дублировал их:
  * force_utf8  — консоль Windows по умолчанию cp1252 и падает на кириллице;
  * Heartbeat   — «процесс жив» во время долгой генерации (cold start).
"""

from __future__ import annotations

import sys
import threading
import time


def force_utf8() -> None:
    """Переводит stdout/stderr в UTF-8 (на Linux/macOS — no-op)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


class Heartbeat:
    """
    Печатает «генерация идёт, прошло N с» каждые interval секунд в фоне.

    Нужен, чтобы долгий первый вызов (cold start) не выглядел зависанием:
    пользователь видит, что процесс живой, и не жмёт Ctrl+C.
    """

    def __init__(self, interval: float = 15.0, prefix: str = "    ") -> None:
        self.interval = interval
        self.prefix = prefix
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        start = time.perf_counter()
        while not self._stop.wait(self.interval):
            elapsed = time.perf_counter() - start
            print(
                f"{self.prefix}…генерация идёт, прошло {elapsed:.0f} с "
                "(первый вызов дольше — не прерываю)",
                flush=True,
            )

    def __enter__(self) -> Heartbeat:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)
