"""
Классификация охвата события: массовое или индивидуальное.

В календарь вуза попадают мероприятия, общие для всех: лекции, конференции,
семинары, дедлайны, экзамены. Индивидуальные консультации преподавателей
(«за попереднім записом», «години прийому доц. Іваненка») в общий календарь
не идут — они касаются одного студента и засоряют ленту.

ПОЧЕМУ ЭТО НЕ В ПРОМТЕ. Решение скрыть данные должно быть детерминированным,
воспроизводимым и объяснимым: по логу видно, какой именно маркер сработал.
Модель с точностью ~80% для такого решения не годится — она молча теряла бы
настоящие анонсы. Тип consultation модель проставить может, но САМ ПО СЕБЕ
поводом отсеять он не является: консультация перед экзаменом для потока —
массовое мероприятие.

КОНСЕРВАТИВНОСТЬ. Отсекаем, только если есть явный маркер индивидуального И
нет ни одного контрмаркера массовости. По умолчанию событие остаётся: ложно
сохранить дешевле, чем ложно потерять — лишнее уберёт модератор, а потерянное
не увидит никто.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.models.schemas import ExtractedEvent

PUBLIC = "public"  # открыто для всех
GROUP = "group"  # для курса/группы/потока — тоже идёт в календарь
INDIVIDUAL = "individual"  # персонально, по записи — в календарь НЕ идёт

# Явные признаки персонального формата.
INDIVIDUAL_MARKERS: tuple[tuple[str, str], ...] = (
    (r"індивідуальн\w*\s+консультац", "індивідуальні консультації"),
    (r"индивидуальн\w*\s+консультац", "индивидуальные консультации"),
    (r"за\s+попереднім\s+записом", "за попереднім записом"),
    (r"за\s+предварительной\s+записью", "за предварительной записью"),
    (r"\bза\s+записом\b", "за записом"),
    (r"запис\w*\s+на\s+консультац", "запис на консультацію"),
    (r"запис\w*\s+до\s+викладач", "запис до викладача"),
    (r"особист\w*\s+прийом", "особистий прийом"),
    (r"годин\w*\s+прийому", "години прийому"),
    (r"прийомн\w*\s+годин", "прийомні години"),
    (r"графік\s+консультацій\s+викладач", "графік консультацій викладачів"),
    (r"у\s+зручний\s+для\s+вас\s+час", "у зручний для вас час"),
    (r"в\s+удобное\s+для\s+вас\s+время", "в удобное для вас время"),
    (r"персональн\w*\s+консультац", "персональні консультації"),
)

# Признаки массовости — перевешивают маркеры выше.
COLLECTIVE_MARKERS: tuple[tuple[str, str], ...] = (
    (r"для\s+всіх", "для всіх"),
    (r"для\s+всех", "для всех"),
    (r"усі\s+охоч", "усі охочі"),
    (r"всі\s+охоч", "всі охочі"),
    (r"все\s+желающ", "все желающие"),
    (r"запрошуємо\s+всіх", "запрошуємо всіх"),
    (r"приглашаем\s+всех", "приглашаем всех"),
    (r"обов[’'`]?язков\w*\s+(?:явка|відвідуванн|присутн)", "обов'язкова явка"),
    (r"явка\s+обов", "явка обов'язкова"),
    (r"для\s+студентів\s+\d", "для студентів N курсу"),
    (r"для\s+\d\s*(?:-го\s*)?курс", "для N курсу"),
    (r"для\s+груп", "для груп"),
    (r"\bпотік\w*\b", "потік"),
    (r"відкрит\w*\s+лекц", "відкрита лекція"),
    (r"загальн\w*\s+збор", "загальні збори"),
)

_INDIVIDUAL_RES = tuple((re.compile(p, re.IGNORECASE), label) for p, label in INDIVIDUAL_MARKERS)
_COLLECTIVE_RES = tuple((re.compile(p, re.IGNORECASE), label) for p, label in COLLECTIVE_MARKERS)


@dataclass
class ScopeVerdict:
    """Решение по охвату + чем оно обосновано (идёт в лог)."""

    scope: str
    reason: str = ""
    individual_markers: list[str] = field(default_factory=list)
    collective_markers: list[str] = field(default_factory=list)

    @property
    def is_individual(self) -> bool:
        return self.scope == INDIVIDUAL


def _hits(text: str, patterns) -> list[str]:
    return [label for regex, label in patterns if regex.search(text)]


def classify_scope(event: ExtractedEvent, source_text: str) -> ScopeVerdict:
    """
    Определяет охват по маркерам исходного текста и полей события.

    Смотрим и сырой текст, и target_audience: формулировка «за попереднім
    записом» часто попадает именно в аудиторию, а не в тело объявления.
    """
    haystack = " ".join(
        part
        for part in (source_text, event.target_audience, event.event_title, event.description)
        if part
    )

    individual = _hits(haystack, _INDIVIDUAL_RES)
    collective = _hits(haystack, _COLLECTIVE_RES)

    if individual and not collective:
        return ScopeVerdict(
            INDIVIDUAL,
            f"персональний формат: {', '.join(individual[:3])}",
            individual, collective,
        )
    if individual and collective:
        # Спорный случай — оставляем: массовость перевешивает.
        return ScopeVerdict(
            GROUP,
            f"є ознаки персонального ({', '.join(individual[:2])}), але й "
            f"масового ({', '.join(collective[:2])}) — лишаємо",
            individual, collective,
        )
    if collective:
        return ScopeVerdict(PUBLIC, f"масовий захід: {', '.join(collective[:3])}",
                            individual, collective)
    return ScopeVerdict(PUBLIC, "ознак персонального формату немає", individual, collective)


def is_individual(event: ExtractedEvent, source_text: str) -> bool:
    """Короткая форма для пайплайна."""
    return classify_scope(event, source_text).is_individual
