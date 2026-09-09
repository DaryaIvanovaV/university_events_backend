"""
Рендер HTML-отчёта по прогону промта.

Отчёт самодостаточен: один файл, стили и скрипт внутри, без CDN и внешних
зависимостей — открывается двойным кликом и работает офлайн.

Главная идея интерфейса: СЛЕВА оригинал сообщения с подсветкой тех фрагментов,
которые подтверждают извлечённые значения, СПРАВА — сами значения со статусом.
Что не подсвечено слева, но стоит справа — модель взяла из воздуха. Это
единственный способ проверять галлюцинации глазами быстро.

Вердикты (ok / выдумка / пропуск) проставляются прямо в отчёте, хранятся в
localStorage браузера и выгружаются кнопкой в verdicts.jsonl для
scripts/review_stats.py. Тот же файл можно загрузить обратно: localStorage
привязан к одному браузеру, а разметку нужно переносить между машинами и
восстанавливать после очистки хранилища.
"""

from __future__ import annotations

import html
import json
import statistics
from typing import Any

from app.eval import pipeline_sim
from app.eval.grounding import EMPTY, INFO, INVALID, INVENTED, MISSED, OK, SUSPECT

# Автоматические статусы. Эвристика умеет сказать «этого нет в тексте», но НЕ
# умеет отличить удачную догадку от вранья — поэтому «вигадана?» со знаком
# вопроса: подтип выбирает человек кнопками.
STATUS_LABEL = {
    OK: "з тексту",
    SUSPECT: "вигадана?",
    INVENTED: "вигадана?",
    INVALID: "формат",
    MISSED: "пропущена",
    EMPTY: "порожня",
    INFO: "—",
}

# Ручные пометки: то, что человек ставит поверх автоматики.
#   correct       — информация полностью взята из текста
#   invented_ok   — модель домыслила, но получилось верно
#   invented_bad  — модель домыслила и ошиблась
#   missed        — данные в тексте были, поле осталось пустым
MARK_CORRECT = "correct"
MARK_INVENTED_OK = "invented_ok"
MARK_INVENTED_BAD = "invented_bad"
MARK_MISSED = "missed"

MARK_LABEL = {
    MARK_CORRECT: "з тексту",
    MARK_INVENTED_OK: "вигадана ✓",
    MARK_INVENTED_BAD: "вигадана ✗",
    MARK_MISSED: "пропущена",
}

# Кнопки в строке поля: (значение, символ, подсказка).
MARK_BUTTONS = (
    (MARK_CORRECT, "✓", "правильна — взята з тексту"),
    (MARK_INVENTED_OK, "≈", "вигадана, але правильна"),
    (MARK_INVENTED_BAD, "✗", "вигадана і неправильна"),
    (MARK_MISSED, "○", "пропущена — у тексті було, поле порожнє"),
)

FIELD_LABEL = {
    "event_title": "Назва",
    "date": "Дата",
    "time": "Час",
    "location": "Місце",
    "organizer": "Організатор",
    "target_audience": "Аудиторія",
    "event_type": "Тип",
    "description": "Опис",
    "language": "Мова",
    "link": "Посилання",
    "meeting_code": "Код зустрічі",
    "events": "Події",
}

_CSS = """
:root {
  --bg: #f6f7f9; --fg: #1b1f24; --card: #ffffff; --border: #d8dde3;
  --muted: #6b7480; --accent: #2f6feb;
  --ok-bg: #e4f5e9; --ok-fg: #1a7f37;
  --suspect-bg: #fff5d9; --suspect-fg: #8a6100;
  --invented-bg: #ffe3e3; --invented-fg: #b42318;
  --missed-bg: #e2eefc; --missed-fg: #1f5aa8;
  --empty-bg: #eef0f3; --empty-fg: #6b7480;
  --mark: #fff3a3;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #14171c; --fg: #e6e9ee; --card: #1c2027; --border: #2e343d;
    --muted: #99a2b0; --accent: #6ea1ff;
    --ok-bg: #14331f; --ok-fg: #6fd08c;
    --suspect-bg: #38300f; --suspect-fg: #e6c05a;
    --invented-bg: #3d1a1a; --invented-fg: #ff8f85;
    --missed-bg: #16283f; --missed-fg: #79b0f5;
    --empty-bg: #23272e; --empty-fg: #99a2b0;
    --mark: #5b5222;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 24px; background: var(--bg); color: var(--fg);
  font: 15px/1.55 -apple-system, "Segoe UI", Roboto, Arial, sans-serif;
}
h1 { font-size: 22px; margin: 0 0 4px; }
a { color: var(--accent); }
.meta { color: var(--muted); font-size: 13px; margin-bottom: 16px; }
.meta code { background: var(--empty-bg); padding: 1px 5px; border-radius: 4px; }
.panel {
  background: var(--card); border: 1px solid var(--border); border-radius: 10px;
  padding: 14px 16px; margin-bottom: 16px;
}
.summary-grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 8px 20px; font-size: 14px;
}
.summary-grid b { font-size: 18px; display: block; font-variant-numeric: tabular-nums; }
.summary-grid span { color: var(--muted); font-size: 12px; }
.toolbar { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-top: 12px; }
button {
  font: inherit; padding: 7px 13px; border-radius: 7px; cursor: pointer;
  border: 1px solid var(--border); background: var(--card); color: var(--fg);
}
button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
button:hover { filter: brightness(0.97); }
#progress { color: var(--muted); font-size: 13px; }
details { margin-top: 10px; }
summary { cursor: pointer; color: var(--accent); font-size: 13px; }
pre {
  background: var(--empty-bg); padding: 10px; border-radius: 7px; overflow-x: auto;
  font: 12.5px/1.5 ui-monospace, Consolas, "Courier New", monospace;
  white-space: pre-wrap; word-break: break-word; margin: 8px 0 0;
}
.card {
  background: var(--card); border: 1px solid var(--border); border-radius: 10px;
  padding: 14px 16px; margin-bottom: 14px;
}
.card.bad { border-left: 4px solid var(--invented-fg); }
.card.warn { border-left: 4px solid var(--suspect-fg); }
.card.good { border-left: 4px solid var(--ok-fg); }
.card-head {
  display: flex; gap: 12px; flex-wrap: wrap; align-items: baseline;
  color: var(--muted); font-size: 12.5px; margin-bottom: 10px;
}
.card-head .idx { color: var(--fg); font-weight: 600; font-size: 14px; }
.cols { display: grid; grid-template-columns: minmax(280px, 1fr) minmax(340px, 1.15fr); gap: 18px; }
@media (max-width: 900px) { .cols { grid-template-columns: 1fr; } }
.source { white-space: pre-wrap; word-break: break-word; }
.source mark { background: var(--mark); color: inherit; border-radius: 3px; padding: 0 1px; }
.col-title { font-size: 12px; text-transform: uppercase; letter-spacing: .04em;
  color: var(--muted); margin-bottom: 6px; }
table { width: 100%; border-collapse: collapse; font-size: 13.5px; }
td { padding: 4px 6px; vertical-align: top; border-bottom: 1px solid var(--border); }
td.name { color: var(--muted); width: 96px; white-space: nowrap; }
td.val { word-break: break-word; }
td.st { width: 84px; white-space: nowrap; }
td.mk { width: 104px; text-align: right; white-space: nowrap; }
button.mark {
  padding: 0 5px; font-size: 12px; line-height: 1.5; border-radius: 5px;
  border: 1px solid var(--border); background: transparent; color: var(--muted);
}
button.mark:hover { color: var(--fg); }
button.mark.on[data-v="correct"] { background: var(--ok-bg); color: var(--ok-fg); border-color: var(--ok-fg); }
button.mark.on[data-v="invented_ok"] { background: var(--suspect-bg); color: var(--suspect-fg); border-color: var(--suspect-fg); }
button.mark.on[data-v="invented_bad"] { background: var(--invented-bg); color: var(--invented-fg); border-color: var(--invented-fg); }
button.mark.on[data-v="missed"] { background: var(--missed-bg); color: var(--missed-fg); border-color: var(--missed-fg); }
.chip.ovr { outline: 1px dashed currentColor; outline-offset: 1px; }
.chip.correct { background: var(--ok-bg); color: var(--ok-fg); }
.chip.invented_ok { background: var(--suspect-bg); color: var(--suspect-fg); }
.chip.invented_bad { background: var(--invented-bg); color: var(--invented-fg); }
.send { margin-left: auto; display: inline-flex; gap: 5px; align-items: center; font-size: 12px; }
.send b { color: var(--muted); font-weight: 500; }
button.sendbtn {
  padding: 1px 8px; font-size: 12px; border-radius: 20px;
  border: 1px solid var(--border); background: transparent; color: var(--muted);
}
button.sendbtn.on[data-v="yes"] { background: var(--ok-bg); color: var(--ok-fg); border-color: var(--ok-fg); }
button.sendbtn.on[data-v="no"] { background: var(--invented-bg); color: var(--invented-fg); border-color: var(--invented-fg); }
/* Система отправит, а рецензент отклонил — это и есть цена ошибок модели. */
.destiny.conflict { background: var(--invented-bg); border-radius: 7px; padding: 5px 8px; }
.destiny { display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; margin: 6px 0 4px; }
.dchip {
  display: inline-block; padding: 1px 8px; border-radius: 20px; font-size: 11.5px;
  font-weight: 600;
}
.dchip.saved { background: var(--missed-bg); color: var(--missed-fg); }
.dchip.skipped { background: var(--empty-bg); color: var(--empty-fg); }
.chip {
  display: inline-block; padding: 1px 7px; border-radius: 20px; font-size: 11.5px;
  background: var(--empty-bg); color: var(--empty-fg);
}
.chip.ok { background: var(--ok-bg); color: var(--ok-fg); }
.chip.suspect { background: var(--suspect-bg); color: var(--suspect-fg); }
.chip.invented, .chip.invalid { background: var(--invented-bg); color: var(--invented-fg); }
.chip.missed { background: var(--missed-bg); color: var(--missed-fg); }
.note { color: var(--muted); font-size: 12px; display: block; }
.null { color: var(--muted); font-style: italic; }
.ev-title { font-size: 12px; color: var(--muted); margin: 10px 0 2px; }
.verdict {
  margin-top: 12px; padding-top: 10px; border-top: 1px dashed var(--border);
  display: flex; gap: 14px; align-items: center; flex-wrap: wrap; font-size: 13.5px;
}
.verdict label { cursor: pointer; user-select: none; }
.verdict input[type=text] {
  flex: 1; min-width: 180px; font: inherit; padding: 5px 8px;
  border: 1px solid var(--border); border-radius: 6px;
  background: var(--bg); color: var(--fg);
}
.expect {
  border: 1px dashed var(--border); border-radius: 7px; padding: 7px 10px;
  margin-bottom: 10px; font-size: 13px; color: var(--muted); background: var(--empty-bg);
}
.expect b { color: var(--fg); }
.msg-checks { margin-top: 8px; }
.msg-checks div { font-size: 13px; margin-bottom: 3px; }
.error { color: var(--invented-fg); font-weight: 600; }
body.only-problems .card.good { display: none; }
body.only-verdict-missing .card.done { display: none; }
body.only-db .card:not([data-db="saved"]) { display: none; }
"""

_JS = r"""
(function () {
  var RUN = document.body.dataset.runId;
  var KEY = "promptcheck:" + RUN;
  var state = {};
  try { state = JSON.parse(localStorage.getItem(KEY) || "{}"); } catch (e) { state = {}; }

  function cards() { return Array.prototype.slice.call(document.querySelectorAll(".card")); }

  function save() {
    try { localStorage.setItem(KEY, JSON.stringify(state)); } catch (e) {}
    cards().forEach(function (card) {
      // Карточка «пройдена», когда у всех её событий выбрана оценка.
      var boxes = card.querySelectorAll(".send");
      var s = state[card.dataset.index];
      // Нет событий — нечего и оценивать, карточка считается пройденной.
      var done = Array.prototype.every.call(boxes, function (box) {
        return s && s.send && s.send[box.dataset.sendKey];
      });
      card.classList.toggle("done", done);
    });
    retally();
  }

  function entry(idx) {
    if (!state[idx]) state[idx] = { field_marks: {}, send: {}, comment: "" };
    if (!state[idx].field_marks) state[idx].field_marks = {};
    if (!state[idx].send) state[idx].send = {};
    return state[idx];
  }

  // Автоматические статусы. "вигадана?" со знаком вопроса: эвристика видит,
  // что значения нет в тексте, но не знает, угадала модель или соврала.
  var LABEL = { ok: "з тексту", suspect: "вигадана?", invented: "вигадана?",
                invalid: "формат", missed: "пропущена", empty: "порожня",
                info: "—",
                correct: "з тексту", invented_ok: "вигадана ✓",
                invented_bad: "вигадана ✗" };

  // Ручная пометка перекрывает автоматику.
  function effective(row) {
    var idx = row.closest(".card").dataset.index;
    var s = state[idx];
    var mark = s && s.field_marks ? s.field_marks[row.dataset.key] : null;
    return mark || row.dataset.auto;
  }

  // Сведение любого статуса к четырём категориям пользователя.
  function bucket(status) {
    if (status === "correct" || status === "ok") return "correct";
    if (status === "invented_ok") return "invented_ok";
    if (status === "invented_bad" || status === "invented" ||
        status === "invalid" || status === "suspect") return "invented_bad";
    if (status === "missed") return "missed";
    return null;  // empty / info — ни о чём не говорят
  }

  function paint(row) {
    var idx = row.closest(".card").dataset.index;
    var s = state[idx];
    var mark = s && s.field_marks ? s.field_marks[row.dataset.key] : null;
    var eff = mark || row.dataset.auto;
    var chip = row.querySelector(".chip");
    chip.className = "chip " + eff + (mark ? " ovr" : "");
    chip.textContent = (LABEL[eff] || eff) + (mark ? " ✎" : "");
    row.querySelectorAll("button.mark").forEach(function (b) {
      b.classList.toggle("on", b.dataset.v === mark);
    });
  }

  function paintSend(box) {
    var idx = box.closest(".card").dataset.index;
    var s = state[idx];
    var choice = s && s.send ? s.send[box.dataset.sendKey] : null;
    box.querySelectorAll("button.sendbtn").forEach(function (b) {
      b.classList.toggle("on", b.dataset.v === choice);
    });
    // Система отправит, а человек отклонил — подсвечиваем расхождение.
    box.closest(".destiny").classList.toggle(
      "conflict", box.dataset.inDb === "1" && choice === "no");
  }

  // Перерисовать весь отчёт из state — нужно после массовой замены
  // (сброс, импорт), когда точечная перерисовка не годится.
  function repaintAll() {
    document.querySelectorAll("tr[data-key]").forEach(function (r) { paint(r); });
    document.querySelectorAll(".send").forEach(function (b) { paintSend(b); });
    cards().forEach(function (card) {
      var input = card.querySelector("input.comment");
      var s = state[card.dataset.index];
      if (input) input.value = (s && s.comment) || "";
    });
  }

  function retally() {
    var counts = { correct: 0, invented_ok: 0, invented_bad: 0, missed: 0 };
    document.querySelectorAll("tr[data-key]").forEach(function (row) {
      var b = bucket(effective(row));
      if (b) counts[b]++;
    });
    document.getElementById("t-correct").textContent = counts.correct;
    document.getElementById("t-inv-ok").textContent = counts.invented_ok;
    document.getElementById("t-inv-bad").textContent = counts.invented_bad;
    document.getElementById("t-missed").textContent = counts.missed;

    // Сколько событий система отправит в БД, а рецензент отклонил.
    var rejected = 0, pending = 0;
    document.querySelectorAll(".send").forEach(function (box) {
      if (box.dataset.inDb !== "1") return;
      var idx = box.closest(".card").dataset.index;
      var s = state[idx];
      var choice = s && s.send ? s.send[box.dataset.sendKey] : null;
      if (choice === "no") rejected++;
      if (!choice) pending++;
    });
    document.getElementById("t-rejected").textContent = rejected;
    document.getElementById("progress").textContent =
      "Подій без оцінки: " + pending;
  }

  cards().forEach(function (card) {
    var idx = card.dataset.index;
    var s = state[idx];
    card.querySelectorAll("tr[data-key]").forEach(function (row) {
      paint(row);
      row.querySelectorAll("button.mark").forEach(function (b) {
        b.addEventListener("click", function () {
          var e = entry(idx);
          // Повторный клик по активной кнопке возвращает авто-статус.
          if (e.field_marks[row.dataset.key] === b.dataset.v) {
            delete e.field_marks[row.dataset.key];
          } else {
            e.field_marks[row.dataset.key] = b.dataset.v;
          }
          paint(row);
          save();
        });
      });
    });
    card.querySelectorAll(".send").forEach(function (box) {
      paintSend(box);
      box.querySelectorAll("button.sendbtn").forEach(function (b) {
        b.addEventListener("click", function () {
          var e = entry(idx);
          if (e.send[box.dataset.sendKey] === b.dataset.v) {
            delete e.send[box.dataset.sendKey];
          } else {
            e.send[box.dataset.sendKey] = b.dataset.v;
          }
          paintSend(box);
          save();
        });
      });
    });
    var comment = card.querySelector("input.comment");
    if (comment) {
      if (s && s.comment) comment.value = s.comment;
      comment.addEventListener("input", function () { entry(idx).comment = comment.value; save(); });
    }
  });

  document.getElementById("filter").addEventListener("click", function () {
    document.body.classList.toggle("only-problems");
    this.textContent = document.body.classList.contains("only-problems")
      ? "Показати всі" : "Лише підозрілі";
  });

  document.getElementById("filter-todo").addEventListener("click", function () {
    document.body.classList.toggle("only-verdict-missing");
    this.textContent = document.body.classList.contains("only-verdict-missing")
      ? "Показати всі" : "Лише без оцінки";
  });

  document.getElementById("download").addEventListener("click", function () {
    var lines = cards().map(function (card) {
      var idx = card.dataset.index;
      var s = state[idx] || {};
      var marks = s.field_marks || {};
      var sends = s.send || {};

      // Итог по каждому полю: ручная пометка или авто-статус.
      var counts = { correct: 0, invented_ok: 0, invented_bad: 0, missed: 0 };
      var bad = [];
      card.querySelectorAll("tr[data-key]").forEach(function (row) {
        var eff = marks[row.dataset.key] || row.dataset.auto;
        var b = bucket(eff);
        if (b) counts[b]++;
        if (b === "invented_bad") bad.push(row.dataset.key.split(".").pop());
      });

      // Оценка по каждому событию карточки.
      var events = [];
      card.querySelectorAll(".send").forEach(function (box, i) {
        events.push({
          ordinal: i,
          in_db: box.dataset.inDb === "1",
          send: sends[box.dataset.sendKey] || null
        });
      });

      // verdict/bad_fields сохраняются ради совместимости со старыми
      // файлами вердиктов, которые читает review_stats.
      var anyBad = counts.invented_bad > 0;
      var anyMiss = counts.missed > 0;
      var verdict = anyBad && anyMiss ? "both"
                  : anyBad ? "hallucination"
                  : anyMiss ? "miss"
                  : (events.length && events.every(function (e) { return e.send; })) ? "ok"
                  : null;

      return JSON.stringify({
        run_id: RUN,
        index: parseInt(idx, 10),
        message_id: card.dataset.messageId ? parseInt(card.dataset.messageId, 10) : null,
        auto_worst: card.dataset.worst,
        in_db: card.dataset.db === "saved",
        events: events,
        field_counts: counts,
        field_marks: marks,
        bad_fields: bad,
        cleared_flags: Object.keys(marks).filter(function (k) {
          return marks[k] === "correct" || marks[k] === "invented_ok";
        }).length,
        verdict: verdict,
        comment: s.comment || "",
        text: card.dataset.preview || ""
      });
    });
    var blob = new Blob([lines.join("\n") + "\n"], { type: "application/x-ndjson;charset=utf-8" });
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "verdicts_" + RUN + ".jsonl";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 1000);
  });

  // Импорт вердиктов из файла. Без него разметка заперта в localStorage
  // одного браузера: её не перенести на другую машину, не восстановить
  // после очистки хранилища и не продолжить вдвоём.
  function applyVerdicts(text) {
    var byIndex = {}, byMessage = {};
    cards().forEach(function (card) {
      byIndex[card.dataset.index] = card;
      if (card.dataset.messageId) byMessage[card.dataset.messageId] = card;
    });
    var known = {};
    document.querySelectorAll("tr[data-key]").forEach(function (r) {
      known[r.dataset.key] = 1;
    });

    // touched — чтобы две строки про одно сообщение не сосчитались дважды.
    var stat = { cards: 0, fields: 0, sends: 0, skipped: 0 }, touched = {};
    text.split(/\r?\n/).forEach(function (line) {
      line = line.trim();
      if (!line) return;
      var rec;
      try { rec = JSON.parse(line); } catch (e) { stat.skipped++; return; }
      // Индекс — основной ключ; message_id страхует, если отчёт пересобран
      // с другим порядком карточек.
      var card = byIndex[rec.index];
      if (!card && rec.message_id !== null && rec.message_id !== undefined) {
        card = byMessage[rec.message_id];
      }
      if (!card) { stat.skipped++; return; }
      var idx = card.dataset.index;
      if (!touched[idx]) { touched[idx] = 1; stat.cards++; }
      var e = entry(idx);
      var marks = rec.field_marks || {};
      Object.keys(marks).forEach(function (k) {
        // Ключ поля — "<индекс карточки>.ev<n>.<поле>". Из чужого прогона
        // индекс может не совпасть: переносим хвост ключа на эту карточку.
        var key = known[k] ? k : idx + "." + k.split(".").slice(1).join(".");
        if (known[key]) { e.field_marks[key] = marks[k]; stat.fields++; }
        else stat.skipped++;
      });
      // Оценки событий выгружаются по порядковому номеру, а не по ключу.
      var boxes = card.querySelectorAll(".send");
      (rec.events || []).forEach(function (ev) {
        if (!ev || !ev.send) return;
        var box = boxes[ev.ordinal];
        if (box) { e.send[box.dataset.sendKey] = ev.send; stat.sends++; }
        else stat.skipped++;
      });
      if (rec.comment) e.comment = rec.comment;
    });
    repaintAll();
    save();
    return stat;
  }

  var importInput = document.getElementById("import-file");

  document.getElementById("import").addEventListener("click", function () {
    // Иначе повторный выбор того же файла не даст события change.
    importInput.value = "";
    importInput.click();
  });

  importInput.addEventListener("change", function () {
    var file = importInput.files && importInput.files[0];
    if (!file) return;
    var reader = new FileReader();
    reader.onerror = function () {
      document.getElementById("import-note").textContent = "Не вдалося прочитати файл";
    };
    reader.onload = function () {
      var text = String(reader.result || "");
      var foreign = text.split(/\r?\n/).some(function (l) {
        if (!l.trim()) return false;
        try { return JSON.parse(l).run_id !== RUN; } catch (e) { return false; }
      });
      if (foreign && !confirm(
            "Файл із ІНШОГО прогону. Позначки прив'язані до конкретних полів " +
            "конкретних відповідей моделі, тому частина може не збігтися. " +
            "Імпортувати?")) return;
      var s = applyVerdicts(text);
      document.getElementById("import-note").textContent =
        "Імпортовано: повідомлень " + s.cards + ", полів " + s.fields +
        ", подій " + s.sends + (s.skipped ? ", не збіглося " + s.skipped : "");
    };
    reader.readAsText(file, "utf-8");
  });

  document.getElementById("reset").addEventListener("click", function () {
    if (!confirm("Стерти всі проставлені вердикти цього прогону?")) return;
    state = {};
    try { localStorage.removeItem(KEY); } catch (e) {}
    document.getElementById("import-note").textContent = "";
    repaintAll();
    save();
  });

  document.getElementById("filter-db").addEventListener("click", function () {
    document.body.classList.toggle("only-db");
    this.textContent = document.body.classList.contains("only-db")
      ? "Показати всі" : "Лише те, що піде в БД";
  });

  save();
})();
"""

# Особенности схемы и промта, которые в отчёте выглядят как ошибка модели,
# но ею не являются. Показываем прямо в отчёте, чтобы вердикты не искажались.
_CAVEATS = [
    (
        "«майстер-клас», «хакатон», «тренінг» → seminar / other",
        "У EventType (app/models/schemas.py) немає таких значень, тому модель "
        "фізично не може повернути щось інше. Це обмеження схеми, не помилка.",
    ),
    (
        "Діапазон дат («15-16 травня») → одна дата",
        "Поле date одне. Модель мусить вибрати початок діапазону — втрата за "
        "дизайном схеми.",
    ),
    (
        "language: «ru» на російських повідомленнях",
        "Системний промт (app/llm/prompts.py) вимагає «uk» або «en», але "
        "few-shot приклад повертає «ru». Інструкція суперечлива — якщо модель "
        "плутається з мовою, причина тут.",
    ),
    (
        "Порожній event_title / time «25:99» / link не-URL",
        "У ExtractedEvent немає жодного валідатора, Pydantic таке пропускає. "
        "Тому формат перевіряє цей звіт (статус «формат»), а не схема.",
    ),
]


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _highlight(text: str, labeled: list[tuple[int, int, str]]) -> str:
    """Оборачивает подтверждённые фрагменты в <mark> с подсказкой о поле."""
    n = len(text)
    if not labeled or n == 0:
        return _esc(text)
    marks: list[set[str]] = [set() for _ in range(n)]
    for start, end, label in labeled:
        for i in range(max(0, start), min(n, end)):
            marks[i].add(label)

    out: list[str] = []
    i = 0
    while i < n:
        current = marks[i]
        j = i
        while j < n and marks[j] == current:
            j += 1
        chunk = _esc(text[i:j])
        if current:
            title = _esc(", ".join(FIELD_LABEL.get(f, f) for f in sorted(current)))
            out.append(f'<mark title="{title}">{chunk}</mark>')
        else:
            out.append(chunk)
        i = j
    return "".join(out)


def compute_stats(records: list[dict]) -> dict[str, Any]:
    """Сводные числа по прогону (используются и в отчёте, и в консоли)."""
    total = len(records)
    events = sum(len(r["events"]) for r in records)
    first_try = sum(1 for r in records if r["valid_json"] and r["attempts"] == 1)
    errors = sum(1 for r in records if r["error"])
    latencies = [r["latency_s"] for r in records if r["latency_s"]]

    counts: dict[str, int] = {}
    for record in records:
        for group in record["event_checks"]:
            for check in group:
                counts[check["status"]] = counts.get(check["status"], 0) + 1
        for check in record["message_checks"]:
            counts[check["status"]] = counts.get(check["status"], 0) + 1

    return {
        "total": total,
        "events": events,
        "first_try": first_try,
        "first_try_pct": (100.0 * first_try / total) if total else 0.0,
        "errors": errors,
        "invented": counts.get(INVENTED, 0) + counts.get(INVALID, 0),
        "suspect": counts.get(SUSPECT, 0),
        "missed": counts.get(MISSED, 0),
        "median_s": statistics.median(latencies) if latencies else 0.0,
        "max_s": max(latencies) if latencies else 0.0,
    }


def _card_class(worst: str) -> str:
    if worst in (INVENTED, INVALID):
        return "bad"
    if worst in (SUSPECT, MISSED):
        return "warn"
    return "good"


def _render_check_row(check: dict, *, key: str | None = None) -> str:
    """
    Строка поля. key задан → доступно ручное переопределение авто-статуса.

    Автоматика — эвристика и ошибается в обе стороны: даёт ложные тревоги и не
    видит выдумок, у которых все поля подтверждены текстом. Поэтому статус
    можно снять (✓) или проставить (✗) руками, а сводка наверху пересчитывается
    по эффективным статусам.
    """
    status = check["status"]
    value = check["value"]
    if value is None or value == "":
        shown = '<span class="null">null</span>'
    elif isinstance(value, (list, dict)):
        shown = _esc(json.dumps(value, ensure_ascii=False))
    else:
        shown = _esc(value)
    note = f'<span class="note">{_esc(check["note"])}</span>' if check["note"] else ""

    if key:
        buttons = "".join(
            f'<button class="mark" data-v="{value}" title="{_esc(hint)}">{sym}</button>'
            for value, sym, hint in MARK_BUTTONS
        )
        marks = f'<td class="mk">{buttons}</td>'
        attrs = f' data-key="{_esc(key)}" data-auto="{_esc(status)}"'
    else:
        marks = '<td class="mk"></td>'
        attrs = ""

    return (
        f"<tr{attrs}>"
        f'<td class="name">{_esc(FIELD_LABEL.get(check["field"], check["field"]))}</td>'
        f'<td class="val">{shown}{note}</td>'
        f'<td class="st"><span class="chip {_esc(status)}">'
        f"{_esc(STATUS_LABEL.get(status, status))}</span></td>"
        f"{marks}"
        "</tr>"
    )


def _render_destiny(destiny: dict | None, key: str) -> str:
    """
    Что система сделает с событием + что об этом думает человек.

    Слева автоматика: пойдёт ли запись в БД и дальше на модерацию. Справа
    выбор рецензента — стоит ли вообще её туда отправлять. Расхождение этих
    двух решений и есть цена ошибок модели: столько мусора дойдёт до
    администратора, если ничего не менять.
    """
    if not destiny:
        return ""
    db = destiny.get("db", "")
    feed = destiny.get("feed", "")
    saved = db == pipeline_sim.SAVED
    cls = "saved" if saved else "skipped"

    label = _esc(pipeline_sim.DB_LABEL.get(db, db))
    if saved:
        label += " → на модерацію"
    bits = [f'<span class="dchip {cls}">{label}</span>']
    if feed:
        feed_cls = "saved" if feed == pipeline_sim.FEED else "skipped"
        bits.append(
            f'<span class="dchip {feed_cls}">'
            f"{_esc(pipeline_sim.FEED_LABEL.get(feed, feed))}</span>"
        )
    if destiny.get("note"):
        bits.append(f'<span class="note">{_esc(destiny["note"])}</span>')

    bits.append(
        f'<span class="send" data-send-key="{_esc(key)}" data-in-db="{"1" if saved else "0"}">'
        "<b>Відправляти далі?</b>"
        '<button class="sendbtn" data-v="yes" title="подія придатна — у БД і на модерацію">'
        "✅ так</button>"
        '<button class="sendbtn" data-v="no" title="подія не придатна — не відправляти">'
        "⛔ ні</button>"
        "</span>"
    )
    return f'<div class="destiny">{"".join(bits)}</div>'


def _render_card(record: dict) -> str:
    labeled: list[tuple[int, int, str]] = []
    for group in record["event_checks"]:
        for check in group:
            for start, end in check["spans"]:
                labeled.append((start, end, check["field"]))

    head_bits = [
        f'<span class="idx">#{record["index"]}</span>',
        f'id={_esc(record["message_id"])}',
        _esc(record["channel"]),
        f'reference: {_esc(record["reference_date"])}',
        f'{record["latency_s"]:.1f} с',
        f'спроб: {record["attempts"]}',
    ]
    if not record["prefilter"]["is_candidate"]:
        head_bits.append("пре-фільтр: відсіяв би")
    if record["error"]:
        head_bits.append('<span class="error">ПОМИЛКА</span>')

    in_db = any(
        d.get("db") == pipeline_sim.SAVED for d in (record.get("events_destiny") or [])
    )
    parts = [
        f'<article class="card {_card_class(record["worst"])}" '
        f'data-index="{record["index"]}" data-message-id="{_esc(record["message_id"])}" '
        f'data-worst="{_esc(record["worst"])}" '
        f'data-db="{"saved" if in_db else "none"}" '
        f'data-preview="{_esc(record["text"][:160])}">',
        f'<div class="card-head">{" · ".join(head_bits)}</div>',
    ]

    # Ожидаемый результат из датасета (_expect), если он есть. Показываем НАД
    # колонками и отдельным стилем, чтобы не спутать с ответом модели.
    expect = record.get("expect")
    if expect:
        parts.append(f'<div class="expect"><b>Очікується:</b> {_esc(expect)}</div>')

    parts += [
        '<div class="cols">',
        '<div><div class="col-title">Оригінал (підсвічено — підтверджує поле)</div>',
        f'<div class="source">{_highlight(record["text"], labeled)}</div></div>',
        '<div><div class="col-title">Витягнуто моделлю</div>',
    ]

    if record["error"]:
        parts.append(f'<p class="error">{_esc(record["error"])}</p>')
    if not record["events"]:
        parts.append('<p class="null">events: [] — подій не знайдено</p>')

    destinies = record.get("events_destiny") or []
    for i, group in enumerate(record["event_checks"], 1):
        if len(record["event_checks"]) > 1:
            parts.append(f'<div class="ev-title">Подія {i}</div>')
        parts.append(
            _render_destiny(
                destinies[i - 1] if i <= len(destinies) else None,
                f'{record["index"]}.ev{i - 1}',
            )
        )
        # Ключ переопределения включает номер события: в одной карточке может
        # быть несколько событий с одинаковыми именами полей.
        rows = "".join(
            _render_check_row(c, key=f'{record["index"]}.ev{i - 1}.{c["field"]}')
            for c in group
        )
        parts.append(f"<table>{rows}</table>")

    if record["message_checks"]:
        # Тоже с ключом: иначе пересчёт в браузере не увидел бы эти строки и
        # плитки прыгали бы относительно серверной сводки при загрузке.
        rows = "".join(
            _render_check_row(c, key=f'{record["index"]}.msg.{c["field"]}')
            for c in record["message_checks"]
        )
        parts.append('<div class="ev-title">По сообщению в цілому</div>')
        parts.append(f"<table>{rows}</table>")

    parts.append("</div></div>")

    parts.append(
        '<div class="verdict">'
        '<input type="text" class="comment" placeholder="коментар до повідомлення '
        "(необов'язково)\">"
        "</div>"
    )

    raw = "\n\n--- наступна спроба ---\n\n".join(record["raw_responses"])
    parts.append(
        f"<details><summary>Сира відповідь моделі ({record['attempts']} спроб)</summary>"
        f"<pre>{_esc(raw)}</pre></details>"
    )
    pf = record["prefilter"]
    parts.append(
        "<details><summary>Пре-фільтр</summary>"
        f'<pre>кандидат: {pf["is_candidate"]}\nscore: {pf["score"]}\n'
        f'збіги: {_esc(", ".join(pf["matched"]))}</pre></details>'
    )
    parts.append("</article>")
    return "".join(parts)


def render(meta: dict, records: list[dict]) -> str:
    """Собирает полный HTML-отчёт."""
    stats = compute_stats(records)

    dest = pipeline_sim.summarize_destiny(records)
    # Плитки с id пересчитываются в браузере по эффективным статусам
    # (авто-статус + ручные правки) — иначе цифры остаются сырой эвристикой.
    filtered_out = (dest["duplicate"] + dest["filtered"] + dest["individual"]
                + dest["stale"] + dest["language"])
    summary_cells = [
        (None, "Повідомлень", stats["total"], "прогнано через модель"),
        (None, "Подій", stats["events"], "усього витягнуто"),
        ("t-correct", "Правильних полів", "—", "значення взяте з тексту"),
        ("t-inv-ok", "Вигаданих ✓", "—", "модель домислила, але вірно"),
        ("t-inv-bad", "Вигаданих ✗", "—", "домислила і помилилася"),
        ("t-missed", "Пропущених", "—", "дані є в тексті, поле порожнє"),
        ("t-db", "Піде в БД", dest["saved"],
         f"відсіяно: минулих {dest['stale']}, консультацій "
         f"{dest['individual']}, ще {filtered_out - dest['stale'] - dest['individual']}"),
        ("t-rejected", "Ти відхилив", "—", "система відправить, а ти проти"),
        (None, "JSON з 1-ї спроби", f'{stats["first_try_pct"]:.0f}%',
         f'{stats["first_try"]} з {stats["total"]}'),
        (None, "Медіана", f'{stats["median_s"]:.1f} с', f'макс {stats["max_s"]:.1f} с'),
    ]
    summary_html = "".join(
        f'<div><b{f" id={cell_id}" if cell_id else ""}>{_esc(v)}</b>'
        f"{_esc(title)}<br><span>{_esc(sub)}</span></div>"
        for cell_id, title, v, sub in summary_cells
    )

    meta_bits = [
        f'модель: <code>{_esc(meta["model"])}</code>',
        f'temperature: <code>{_esc(meta["temperature"])}</code>',
        f'retries: <code>{_esc(meta["max_retries"])}</code>',
        f'Ollama: <code>{_esc(meta["base_url"])}</code>',
        f'датасет: <code>{_esc(meta["dataset"])}</code>',
        f'запуск: {_esc(meta["started_at"])}',
        f'тривалість: {meta["duration_s"]:.0f} с',
    ]
    if meta.get("warmup_s"):
        meta_bits.append(f'прогрів: {meta["warmup_s"]:.1f} с')

    warnings_html = ""
    if meta.get("warnings"):
        items = "".join(f"<li>{_esc(w)}</li>" for w in meta["warnings"])
        warnings_html = (
            f'<div class="panel"><b>Попередження завантажувача</b><ul>{items}</ul></div>'
        )

    caveats_html = "".join(
        f"<li><b>{_esc(title)}</b><br><span class=\"note\">{_esc(body)}</span></li>"
        for title, body in _CAVEATS
    )

    prompt = meta["prompt"]
    fewshot_html = "".join(
        f'<pre>[{_esc(m["role"])}]\n{_esc(m["content"])}</pre>' for m in prompt["fewshot"]
    )

    cards_html = "".join(_render_card(r) for r in records)

    return f"""<!doctype html>
<html lang="uk">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Перевірка промту — {_esc(meta["model"])} — {_esc(meta["started_at"])}</title>
<style>{_CSS}</style>
</head>
<body data-run-id="{_esc(meta["run_id"])}">
<h1>Перевірка промту: чи не вигадує модель</h1>
<div class="meta">{" · ".join(meta_bits)}</div>

<div class="panel">
  <div class="summary-grid">{summary_html}</div>
  <div class="toolbar">
    <button id="filter">Лише підозрілі</button>
    <button id="filter-db">Лише те, що піде в БД</button>
    <button id="filter-todo">Лише без оцінки</button>
    <button id="download" class="primary">Зберегти verdicts.jsonl</button>
    <button id="import">Завантажити вердикти з файлу</button>
    <input type="file" id="import-file" accept=".jsonl,.ndjson,.json" hidden>
    <button id="reset">Скинути вердикти</button>
    <span id="progress"></span>
    <span id="import-note" class="note"></span>
  </div>
  <div class="note" style="margin-top:8px">
    <b>Позначки поля.</b> Автоматика вміє сказати лише «цього немає в тексті»
    — вгадала модель чи збрехала, вона не знає, тому пише «вигадана?».
    Вирішуєш ти чотирма кнопками:
    <b>✓</b> правильна (взята з тексту) ·
    <b>≈</b> вигадана, але правильна ·
    <b>✗</b> вигадана і неправильна ·
    <b>○</b> пропущена (у тексті було, поле порожнє).
    Повторний клік повертає авто-статус, перевизначене помічається <b>✎</b>.
    <br>
    <b>Оцінка події.</b> Праворуч у синьому рядку — «Відправляти далі?»:
    чи придатна подія, щоб піти в БД і на модерацію. Якщо система її
    відправить, а ти натиснув «⛔ ні», рядок підсвічується червоним — це і є
    ціна помилок моделі.
    <br>
    Підсвічене в оригіналі зліва — те, що підтверджує значення. Плитки вгорі
    перераховуються одразу.
    <br>
    <b>Збереження.</b> Вердикти лежать у localStorage цього браузера.
    «Зберегти verdicts.jsonl» вивантажує їх у файл для
    <code>python -m scripts.review_stats</code>, «Завантажити вердикти з
    файлу» — вносить такий файл назад: так розмітку можна перенести на іншу
    машину або відновити після очищення сховища. Імпорт накладається поверх
    наявного: збіжні позначки перезаписуються, решта лишається.
  </div>
</div>

{warnings_html}

<div class="panel">
  <details>
    <summary>Читати перед оцінкою: що НЕ є помилкою моделі ({len(_CAVEATS)})</summary>
    <ul>{caveats_html}</ul>
  </details>
  <details>
    <summary>Промт, який пішов у модель</summary>
    <pre>[system]\n{_esc(prompt["system"])}</pre>
    {fewshot_html}
    <pre>[user — приклад для першого повідомлення]\n{_esc(prompt["user_example"])}</pre>
  </details>
</div>

{cards_html}

<script>{_JS}</script>
</body>
</html>
"""
