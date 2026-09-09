"""
aiogram-бот: единая точка Telegram-интеграции (без userbot/MTProto).

Три роли:
  1. Live-захват. Бот добавлен АДМИНИСТРАТОРОМ канала → получает каждый
     новый пост (channel_post) и отправляет его в пайплайн /events/ingest.
  2. Модерация (этап из статьи). Каждое извлечённое событие приходит
     администраторам карточкой с кнопками ✅ Підтвердити / ✏️ Редагувати /
     ❌ Відхилити. В приложение и push событие попадает ТОЛЬКО после
     подтверждения.
  3. Ручное добавление. Админ пишет боту в личку текст объявления или
     пересылает старый пост — обработка та же (удобно точечно добавить
     историю).

Историю канала Bot API читать не умеет — для массового импорта старых
постов используйте scripts/import_history.py (JSON-экспорт Telegram Desktop).

Команды в личке: /id — узнать свой chat id, /cancel — отменить правку.

Запуск:  python -m app.ingestion.aiogram_bot
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# --- Помощники без зависимостей от aiogram (покрыты тестами) ---

FIELD_ALIASES: dict[str, str] = {
    "назва": "event_title", "название": "event_title", "title": "event_title",
    "дата": "date", "date": "date",
    "час": "time", "время": "time", "time": "time",
    "місце": "location", "место": "location", "location": "location",
    "організатор": "organizer", "организатор": "organizer",
    "organizer": "organizer",
    "курс": "target_audience", "ца": "target_audience",
    "audience": "target_audience",
    "тип": "type_alias_event_type", "type": "type_alias_event_type",
    "опис": "description", "описание": "description",
    "description": "description",
    "мова": "language", "язык": "language", "language": "language",
    "посилання": "link", "ссылка": "link", "link": "link", "url": "link",
    "код": "meeting_code", "код доступу": "meeting_code",
    "meeting_code": "meeting_code", "id": "meeting_code",
}
# отдельный маркер, чтобы не путать location/type при разборе
FIELD_ALIASES = {
    k: ("event_type" if v == "type_alias_event_type" else v)
    for k, v in FIELD_ALIASES.items()
}

EDIT_HELP = (
    "Надішліть виправлення рядками у форматі «поле: значення»:\n\n"
    "назва: Гостьова лекція Kotlin\n"
    "дата: 2026-05-01\n"
    "час: 14:30\n"
    "місце: Ауд. 405\n"
    "курс: 4 курс\n"
    "тип: lecture\n"
    "посилання: https://example.com/reg\n"
    "код: ID 845 2371 9004 · код 316742\n"
    "опис: -\n\n"
    "«-» очищає поле. Типи: conference, lecture, webinar, seminar, "
    "deadline, exam, consultation, other.\n"
    "/cancel — скасувати."
)


def parse_edit_lines(text: str) -> dict[str, str | None]:
    """'дата: 2026-05-01\\nчас: 14:30' → {'date': ..., 'time': ...}."""
    fields: dict[str, str | None] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        field = FIELD_ALIASES.get(key.strip().casefold())
        value = value.strip()
        if field and value:
            fields[field] = None if value in {"-", "—", "null"} else value
    return fields


def format_card(e: dict) -> str:
    def v(x: object) -> str:
        return str(x) if x else "—"

    return (
        "🆕 Нова подія — очікує підтвердження\n\n"
        f"📌 {e.get('event_title')}\n"
        f"📅 {v(e.get('date'))}   🕐 {v(e.get('time'))}\n"
        f"📍 {v(e.get('location'))}\n"
        f"👥 {v(e.get('target_audience'))}\n"
        f"🏷 {v(e.get('event_type'))}   🌐 {v(e.get('language'))}\n"
        f"🔗 {v(e.get('link'))}\n"
        f"🔑 {v(e.get('meeting_code'))}\n"
        f"id: {e.get('id')}"
    )


def channel_allowed(chat_id: int, username: str | None) -> bool:
    """Белый список каналов; пустой список = разрешены все."""
    allowed = {c.casefold() for c in settings.channels}
    if not allowed:
        return True
    candidates = {str(chat_id)}
    if username:
        candidates.add(f"@{username.casefold()}")
    return bool(candidates & allowed)


# --- Сам бот ---


async def run_bot() -> None:
    from aiogram import Bot, Dispatcher, F
    from aiogram.filters import Command
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.state import State, StatesGroup
    from aiogram.fsm.storage.memory import MemoryStorage
    from aiogram.types import (
        CallbackQuery,
        InlineKeyboardButton,
        InlineKeyboardMarkup,
        Message,
    )

    class EditEvent(StatesGroup):
        waiting = State()

    bot = Bot(token=settings.bot_token)
    dp = Dispatcher(storage=MemoryStorage())
    http = httpx.AsyncClient(
        base_url=settings.api_base_url,
        timeout=60.0,  # запросы к API теперь короткие: инференс делает воркер
        headers=settings.api_headers,
    )

    if not settings.admins:
        logger.warning(
            "ADMIN_CHAT_IDS пуст — карточки модерации слать некому. "
            "Отправьте боту /id и добавьте свой id в .env"
        )

    # Обработка сообщения теперь включает ожидание воркера (секунды, изредка
    # минуты). Держать на это время обработчик апдейта нельзя — бот перестал
    # бы отвечать на кнопки. Поэтому запускаем фоновой задачей.
    background: set[asyncio.Task] = set()

    def spawn(coro) -> None:
        """Фоновая задача с логированием ошибок и защитой от сборщика мусора."""
        task = asyncio.create_task(coro)
        # Без сильной ссылки asyncio может собрать задачу до завершения.
        background.add(task)
        task.add_done_callback(background.discard)

        def _log_failure(t: asyncio.Task) -> None:
            if not t.cancelled() and t.exception() is not None:
                logger.error("Фонова задача впала", exc_info=t.exception())

        task.add_done_callback(_log_failure)

    def is_admin(user_id: int | None) -> bool:
        return user_id is not None and user_id in settings.admins

    def moderation_kb(event_id: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Підтвердити", callback_data=f"approve:{event_id}"
                    ),
                    InlineKeyboardButton(
                        text="✏️ Редагувати", callback_data=f"edit:{event_id}"
                    ),
                    InlineKeyboardButton(
                        text="❌ Відхилити", callback_data=f"reject:{event_id}"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="📄 Оригінал", callback_data=f"orig:{event_id}"
                    ),
                ],
            ]
        )

    async def wait_for_job(job_id: int) -> dict | None:
        """
        Опрашивает GET /jobs/{id}, пока воркер не завершит задачу.

        /events/ingest отвечает 202 сразу: событий на тот момент ещё нет,
        сообщение только поставлено в очередь. Ждём результат здесь, а не
        внутри HTTP-запроса — так один медленный инференс не задерживает
        остальные запросы к API.

        Ожидание неблокирующее (asyncio.sleep): бот всё это время продолжает
        принимать посты и нажатия кнопок.
        """
        deadline = asyncio.get_running_loop().time() + settings.job_poll_timeout_s
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(settings.job_poll_interval_s)
            try:
                resp = await http.get(f"/jobs/{job_id}")
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                logger.warning("Не вдалося прочитати задачу %s: %s", job_id, exc)
                continue
            job = resp.json()
            if job["status"] in ("done", "failed"):
                return job
        logger.warning(
            "Задача %s не завершилася за %.0f с — картки не надсилаю.",
            job_id, settings.job_poll_timeout_s,
        )
        return None

    async def fetch_event(event_id: int) -> dict | None:
        try:
            resp = await http.get(f"/events/{event_id}")
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            logger.warning("Не вдалося прочитати подію %s: %s", event_id, exc)
            return None

    async def ingest_and_moderate(
        text: str,
        source_channel: str,
        source_message_id: int,
        reference_date: str,
        reply_to: Message | None = None,
    ) -> None:
        try:
            resp = await http.post(
                "/events/ingest",
                json={
                    "text": text,
                    "source_channel": source_channel,
                    "source_message_id": source_message_id,
                    "reference_date": reference_date,
                },
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.error("Ошибка /events/ingest: %s", exc)
            if reply_to:
                await reply_to.answer(
                    "⚠️ Backend недоступний — не можу обробити повідомлення.\n"
                    "Запустіть його в окремому терміналі:\n"
                    "python -m uvicorn app.main:app"
                )
            return

        accepted = resp.json()
        job_id = accepted["job_id"]
        if reply_to:
            await reply_to.answer(f"⏳ Прийнято в обробку (задача {job_id})…")

        job = await wait_for_job(job_id)
        if job is None:
            if reply_to:
                await reply_to.answer(
                    "⏱ Обробка триває довше звичайного. Подивіться пізніше — "
                    f"задача {job_id}."
                )
            return
        if job["status"] == "failed":
            logger.error("Задача %s провалена: %s", job_id, job.get("last_error"))
            if reply_to:
                await reply_to.answer(
                    "⚠️ Не вдалося обробити повідомлення.\n"
                    f"Причина: {job.get('last_error') or 'невідома'}"
                )
            return

        events = [
            e for e in [await fetch_event(i) for i in job.get("event_ids", [])] if e
        ]
        pending = [e for e in events if e.get("status") == "pending"]
        if reply_to and not pending:
            # Отсев фильтрами — не ошибка, а штатное поведение (индивідуальні
            # консультації, звіти про минуле). Показываем причину: без неё
            # админ не отличит «відфільтровано» от «загубилося».
            reasons = "\n".join(
                f"• {s['event_title']} — {s['reason']}"
                for s in job.get("skipped", [])
            )
            await reply_to.answer(
                "Подій не знайдено (або вони вже є в базі)."
                + (f"\n\nВідсіяно:\n{reasons}" if reasons else "")
            )
        for event in pending:
            for admin_id in settings.admins:
                try:
                    await bot.send_message(
                        admin_id,
                        format_card(event),
                        reply_markup=moderation_kb(event["id"]),
                    )
                except Exception:
                    logger.exception(
                        "Не вдалося надіслати картку адміну %s", admin_id
                    )

    # 1) Новые посты канала (бот — админ канала).
    @dp.channel_post()
    async def on_channel_post(message: Message) -> None:
        if not channel_allowed(message.chat.id, message.chat.username):
            return
        text = message.text or message.caption
        if not text:
            return
        channel = (
            f"@{message.chat.username}"
            if message.chat.username
            else str(message.chat.id)
        )
        spawn(
            ingest_and_moderate(
                text,
                source_channel=channel,
                source_message_id=message.message_id,
                reference_date=message.date.date().isoformat(),
            )
        )

    # 2) Служебные команды.
    @dp.message(Command("start", "id"))
    async def cmd_id(message: Message) -> None:
        await message.answer(
            f"Ваш chat id: {message.chat.id}\n"
            "Додайте його в ADMIN_CHAT_IDS у .env, щоб отримувати картки "
            "модерації подій."
        )

    @dp.message(Command("cancel"))
    async def cmd_cancel(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("Скасовано.")

    # 3) Ввод правок (в состоянии редактирования) — регистрируем ДО общего.
    @dp.message(EditEvent.waiting, F.chat.type == "private")
    async def on_edit_input(message: Message, state: FSMContext) -> None:
        if not is_admin(message.from_user.id if message.from_user else None):
            return
        fields = parse_edit_lines(message.text or "")
        if not fields:
            await message.answer("Не розпізнав жодного поля.\n\n" + EDIT_HELP)
            return
        data = await state.get_data()
        event_id = data.get("event_id")
        resp = await http.patch(f"/events/{event_id}", json=fields)
        if resp.status_code == 422:
            await message.answer(
                "Некоректні значення (перевірте дату YYYY-MM-DD і тип).\n"
                "Спробуйте ще раз або /cancel"
            )
            return
        if resp.status_code != 200:
            await message.answer("⚠️ Не вдалося оновити подію.")
            await state.clear()
            return
        await state.clear()
        event = resp.json()
        await message.answer(
            "Оновлено:\n\n" + format_card(event),
            reply_markup=moderation_kb(event["id"]),
        )

    # 4) Ручное добавление: любое сообщение админа в личке.
    @dp.message(F.chat.type == "private")
    async def on_private_message(message: Message) -> None:
        if not is_admin(message.from_user.id if message.from_user else None):
            await message.answer(
                "Немає доступу. Дізнатися свій id: команда /id"
            )
            return
        text = message.text or message.caption
        if not text:
            return
        origin = getattr(message, "forward_origin", None)
        origin_date = getattr(origin, "date", None) or message.date
        spawn(
            ingest_and_moderate(
                text,
                source_channel=f"manual:{message.chat.id}",
                source_message_id=message.message_id,
                reference_date=origin_date.date().isoformat(),
                reply_to=message,
            )
        )

    # 5) Кнопки модерации.
    @dp.callback_query(F.data.startswith("approve:"))
    async def cb_approve(callback: CallbackQuery) -> None:
        if not is_admin(callback.from_user.id):
            await callback.answer("Немає доступу", show_alert=True)
            return
        event_id = int(callback.data.split(":", 1)[1])
        resp = await http.post(f"/events/{event_id}/approve")
        if resp.status_code == 200:
            await callback.message.edit_text(
                (callback.message.text or "")
                + "\n\n✅ Підтверджено — опубліковано."
            )
            await callback.answer("Підтверджено")
        else:
            await callback.answer("Подію не знайдено", show_alert=True)

    @dp.callback_query(F.data.startswith("reject:"))
    async def cb_reject(callback: CallbackQuery) -> None:
        if not is_admin(callback.from_user.id):
            await callback.answer("Немає доступу", show_alert=True)
            return
        event_id = int(callback.data.split(":", 1)[1])
        resp = await http.post(f"/events/{event_id}/reject")
        if resp.status_code == 200:
            await callback.message.edit_text(
                (callback.message.text or "") + "\n\n❌ Відхилено."
            )
            await callback.answer("Відхилено")
        else:
            await callback.answer("Подію не знайдено", show_alert=True)

    @dp.callback_query(F.data.startswith("edit:"))
    async def cb_edit(callback: CallbackQuery, state: FSMContext) -> None:
        if not is_admin(callback.from_user.id):
            await callback.answer("Немає доступу", show_alert=True)
            return
        event_id = int(callback.data.split(":", 1)[1])
        await state.set_state(EditEvent.waiting)
        await state.update_data(event_id=event_id)
        await callback.message.answer(EDIT_HELP)
        await callback.answer()

    # Показать оригинал сообщения (raw_text) отдельным сообщением.
    @dp.callback_query(F.data.startswith("orig:"))
    async def cb_orig(callback: CallbackQuery) -> None:
        if not is_admin(callback.from_user.id):
            await callback.answer("Немає доступу", show_alert=True)
            return
        event_id = int(callback.data.split(":", 1)[1])
        try:
            resp = await http.get(f"/events/{event_id}")
            resp.raise_for_status()
        except httpx.HTTPError:
            await callback.answer(
                "Не вдалося отримати оригінал (backend недоступний?)",
                show_alert=True,
            )
            return
        raw = resp.json().get("raw_text")
        if not raw:
            await callback.answer("Оригінал недоступний", show_alert=True)
            return
        # Повний текст — окремим повідомленням (alert обмежений ~200 символами;
        # ліміт повідомлення Telegram — 4096, тому підстраховка обрізанням).
        await callback.message.answer("📄 Оригінал повідомлення:\n\n" + raw[:4000])
        await callback.answer()

    # Проверка backend при старте: если не поднят — карточки модерации не придут.
    # Не падаем (бот может стартовать раньше backend), только громко предупреждаем.
    try:
        r = await http.get("/health", timeout=5.0)
        r.raise_for_status()
        logger.info("Backend доступний (%s).", settings.api_base_url)
    except Exception:
        logger.warning(
            "Backend НЕ доступний на %s — карточки модерації НЕ прийдуть. "
            "Запустіть його в окремому терміналі: python -m uvicorn app.main:app",
            settings.api_base_url,
        )

    try:
        logger.info(
            "Бот запущено. Канали: %s; адміни: %s",
            settings.channels or "усі, де бот адмін",
            settings.admins,
        )
        await dp.start_polling(bot)
    finally:
        await http.aclose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_bot())
