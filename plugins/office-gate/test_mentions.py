#!/usr/bin/env python3
"""Тесты office-gate v3 — детерминированная адресация и арбитр.

Запуск (без pytest и без сети):
    python3 plugins/office-gate/test_mentions.py

Зачем: адресация решает судьбу сообщения БЕЗ обращения к модели, а арбитр
гарантирует «отвечает ровно один». Оба провала живого прогона зафиксированы
здесь кейсами: семеро отвечающих на одно сообщение и реакция всех ботов на
служебную команду вида `/sethome@legalllmbot`.

Первая версия ловила основу «мозг» внутри «мозговой штурм» — отсюда
ограничение на длину окончания в `_mentions`, негативные кейсы ниже держат это.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import threading
from pathlib import Path

# Заглушка httpx до импорта модуля — тест не должен ходить в сеть.
sys.modules.setdefault("httpx", type(sys)("httpx"))

_spec = importlib.util.spec_from_file_location(
    "office_gate_under_test", Path(__file__).with_name("__init__.py")
)
og = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(og)

ROLES = list(og.ROLE_ALIASES)
failures: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        failures.append(f"{label}: получили {got!r}, ожидали {want!r}")


def route(text: str, me: str, bot_username: str = "") -> str | None:
    return og.decide_route(text, me, og._aliases(), bot_username)


# ---------------------------------------------------------------------------
# 1. Обращение по имени: названный отвечает, остальные молчат
# ---------------------------------------------------------------------------

named_cases = [
    ("секретарь, поставь напоминание на пятницу", "secretary"),
    ("секретарю передай, что встреча в 5", "secretary"),
    ("юрист, глянь оферту", "legal"),
    ("вопрос юристу по договору", "legal"),
    ("мозг, найди заметку про хермес", "brain"),
    ("финансист, сколько я потратил", "finance"),
    ("репетитор, объясни производные", "tutor"),
    ("тьютор, давай задачу по алгоритмам", "tutor"),
    ("трекер, что там по проектам", "tracker"),
    ("ресёрчер, найди статью", "research"),
    ("исследователь, проверь источники", "research"),
    ("brain, посмотри в vault", "brain"),
    ("legal, проверь договор", "legal"),
]

for text, owner in named_cases:
    check(f"[имя] {text!r} -> {owner} отвечает", route(text, owner), owner)
    for other in ROLES:
        if other == owner:
            continue
        check(f"[имя] {text!r} -> {other} молчит", route(text, other), "")

# ---------------------------------------------------------------------------
# 2. Ложные срабатывания: слово похоже на имя роли, но обращения нет
# ---------------------------------------------------------------------------

for text in [
    "надо провести мозговой штурм по проекту",
    "мозговая активность вечером падает",
    "финансирование проекта одобрено",
    "юридический адрес компании поменялся",
    "исследовательская работа сдана",
    "преподавательский состав сменился",
]:
    for role in ROLES:
        check(f"[ложное] {text!r} -> {role} к арбитру", route(text, role), None)

# ---------------------------------------------------------------------------
# 3. Без имени — решает арбитр, а не каждый сам за себя
# ---------------------------------------------------------------------------

for text in [
    "что там у меня по деньгам за неделю",
    "какие последние записи по AI team?",
    "привет, как дела",
    "все закройте ебальники",
]:
    for role in ROLES:
        check(f"[без имени] {text!r} -> {role} к арбитру", route(text, role), None)

# ---------------------------------------------------------------------------
# 4. Слэш-команды: отвечает только адресованный бот
# ---------------------------------------------------------------------------

check(
    "[slash] /sethome@legalllmbot -> legal отвечает",
    route("/sethome@legalllmbot", "legal", "legalllmbot"),
    "legal",
)
for role in ROLES:
    if role == "legal":
        continue
    check(
        f"[slash] /sethome@legalllmbot -> {role} молчит",
        route("/sethome@legalllmbot", role, f"{role}_bot"),
        "",
    )
check("[slash] /start без адресата -> к арбитру", route("/start", "secretary", "sec_bot"), None)

# ---------------------------------------------------------------------------
# 5. Арбитр: гонка семи процессов даёт ровно одного отвечающего
# ---------------------------------------------------------------------------

with tempfile.TemporaryDirectory() as tmp:
    os.environ["OFFICE_ROUTING_DB"] = str(Path(tmp) / "routing.db")
    os.environ["OFFICE_DEFAULT_RESPONDER"] = "secretary"

    decide_calls: list[str] = []
    calls_lock = threading.Lock()
    res_lock = threading.Lock()

    def fake_decide() -> str:
        with calls_lock:
            decide_calls.append("x")
        return "finance"

    results: dict[str, str | None] = {}
    barrier = threading.Barrier(len(ROLES))

    def worker(role: str) -> None:
        barrier.wait()  # стартуем одновременно — это и есть гонка
        got = og._claim_or_wait("test:key:1", role, fake_decide)
        with res_lock:
            results[role] = got

    threads = [threading.Thread(target=worker, args=(r,)) for r in ROLES]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    check("[арбитр] модель спрошена ровно один раз", len(decide_calls), 1)
    check("[арбитр] все семеро видят одно решение", set(results.values()), {"finance"})
    check("[арбитр] отвечает ровно один", [r for r, a in results.items() if a == r], ["finance"])

    # Модель недоступна: решение всё равно должно появиться (дежурный), а не
    # выродиться в «ждём таймаут и каждый решает сам» — это и был путь к
    # семи одновременным ответам.
    def never_decides() -> str:
        raise RuntimeError("модель недоступна")

    stuck: dict[str, str | None] = {}

    def worker2(role: str) -> None:
        got = og._claim_or_wait("test:key:2", role, never_decides)
        with res_lock:
            stuck[role] = got

    threads = [threading.Thread(target=worker2, args=(r,)) for r in ROLES]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    check("[сбой] решение подменено дежурным", set(stuck.values()), {"secretary"})
    check(
        "[сбой] отвечает ровно дежурный",
        [r for r, a in stuck.items() if a == r],
        ["secretary"],
    )

# ---------------------------------------------------------------------------
# 6. Липкость треда: короткая реплика без имени продолжает разговор
#    с тем, кто отвечал последним в ЭТОМ топике — не падает на дежурного
# ---------------------------------------------------------------------------


class _FakeSource:
    def __init__(self, chat_id="-100999", thread_id="103", platform="telegram", chat_type="group"):
        self.chat_id = chat_id
        self.thread_id = thread_id
        self.chat_type = chat_type

        class _P:
            value = platform

        self.platform = _P()


class _FakeEvent:
    def __init__(self, text, source, reply_to_is_own_message=False, message_id=None):
        self.text = text
        self.source = source
        self.reply_to_is_own_message = reply_to_is_own_message
        self.message_id = message_id


_real_profile_name = og._profile_name


def _run_gate_as(profile: str, text: str, src, message_id: str):
    """office_gate() от лица конкретного профиля — без плясок с HERMES_HOME."""
    og._profile_name = lambda: profile  # type: ignore[assignment]
    try:
        return og.office_gate(_FakeEvent(text, src, message_id=message_id), None, None)
    finally:
        og._profile_name = _real_profile_name  # type: ignore[assignment]


with tempfile.TemporaryDirectory() as tmp:
    os.environ["OFFICE_ROUTING_DB"] = str(Path(tmp) / "sticky.db")
    os.environ["OFFICE_DEFAULT_RESPONDER"] = "secretary"
    os.environ.pop("OPENROUTER_API_KEY", None)  # арбитр не должен понадобиться

    src = _FakeSource(thread_id="103")  # топик «право»

    # Юрист отвечает на именованное обращение — и запоминается как собеседник.
    r1 = _run_gate_as("legal", "юрист, какой ндс?", src, "m1")
    check("[липкость] юрист отвечает на своё имя", r1, None)

    # Секретарь на ТУ ЖЕ реплику: она с чужим именем — молчит по шагам 3-5,
    # до липкости дело не доходит вовсе (проверка, что порядок шагов верный).
    r2 = _run_gate_as("secretary", "юрист, какой ндс?", src, "m1")
    check("[липкость] именованное обращение к другому — молчим (не по липкости)", r2["action"], "skip")

    # Главный кейс: короткая реплика БЕЗ имени и без свайп-реплая — «да».
    # У юриста она продолжает разговор — он последний отвечавший в этом топике.
    r3 = _run_gate_as("legal", "да", src, "m2")
    check("[липкость] юрист продолжает разговор без имени", r3, None)

    # У секретаря та же реплика — обязан промолчать, а не отвечать «дежурным».
    # Раньше именно так секретарь «перебивал» разговор с юристом.
    r4 = _run_gate_as("secretary", "да", src, "m2")
    check("[липкость] секретарь МОЛЧИТ, а не отвечает дежурным", r4["action"] if r4 else None, "skip")
    if r4:
        check("[липкость] причина — липкость, не дежурный", "sticky" in r4.get("reason", ""), True)

    # Именованное обращение всё равно перебивает липкость к юристу.
    r5 = _run_gate_as("finance", "финансист, а по деньгам как?", src, "m3")
    check("[липкость] именованный финансист отвечает несмотря на липкость к юристу", r5, None)

    # Просрочка: старая запись за пределами окна — липкость больше не действует.
    conn = og._connect()
    conn.execute(
        "UPDATE last_speaker SET at = ? WHERE thread_key = ?",
        (0.0, og._thread_key(src)),
    )
    conn.commit()
    conn.close()
    check("[липкость] просроченная запись игнорируется", og._last_speaker(src), None)

# ---------------------------------------------------------------------------
# 7. Страховка: office_report забыт — плагин шлёт отчёт сам из summary
# ---------------------------------------------------------------------------

sent: list[str] = []


def _fake_send_to_office(text: str) -> str:
    # Повторяет проверку пустой строки из реальной _send_to_office (сеть не
    # трогаем, но пустой-текст сценарий должен вести себя как в реальности).
    text = (text or "").strip()
    if not text:
        return "Ошибка: пустой текст сообщения."
    sent.append(text)
    return "Отправлено в общий чат."


og._send_to_office = _fake_send_to_office  # type: ignore[assignment]

# Карточка закрыта, office_report НЕ вызывался -> страховка обязана сработать.
og._track_kanban_completion(
    tool_name="kanban_complete",
    args={"task_id": "t_abc123", "summary": "Нашёл цену, ссылка в комментарии."},
    result='{"ok": true}',
    task_id="sess-1",
)
og._maybe_auto_report(session_id="sess-1", task_id="")
check("[страховка] сработала при забытом office_report", len(sent), 1)
if sent:
    check("[страховка] текст содержит summary", "Нашёл цену" in sent[0], True)
    check("[страховка] помечен как автоотчёт", sent[0].startswith("🔧 Автоотчёт"), True)

# Карточка закрыта, office_report ПОЗВАН и УСПЕШНО отправил -> без дублей.
sent.clear()
og._track_kanban_completion(
    tool_name="kanban_complete",
    args={"task_id": "t_def456", "summary": "Готово."},
    result='{"ok": true}',
    task_id="sess-2",
)
og._track_kanban_completion(
    tool_name="office_report",
    args={"text": "своими словами"},
    result="Отправлено в общий чат.",
    task_id="sess-2",
)
og._maybe_auto_report(session_id="sess-2", task_id="")
check("[страховка] не дублирует, если модель отчиталась сама", len(sent), 0)

# Карточка закрыта, office_report ПОЗВАН, но ПРОВАЛИЛСЯ (пустой текст, сеть,
# не задан TELEGRAM_BOT_TOKEN и т.п.) -> страховка обязана сработать всё
# равно. Живой баг: модель звала office_report несколько раз подряд с
# незаполненным текстом (сигнатура инструмента была несовместима с тем, как
# Hermes реально зовёт обработчики — args одним словарём, не **kwargs),
# каждый вызов возвращал "Ошибка: пустой текст сообщения", а старая версия
# страховки засчитывала «отчёт был» по самому факту вызова и молчала.
sent.clear()
og._track_kanban_completion(
    tool_name="kanban_complete",
    args={"task_id": "t_ghi789", "summary": "Актуальные параметры НПД на 2026."},
    result='{"ok": true}',
    task_id="sess-2b",
)
og._track_kanban_completion(
    tool_name="office_report",
    args={},
    result="Ошибка: пустой текст сообщения.",
    task_id="sess-2b",
)
og._maybe_auto_report(session_id="sess-2b", task_id="")
check("[страховка] срабатывает, если office_report вызывался, но провалился", len(sent), 1)

# Ошибка в kanban_complete (невалидный task_id и т.п.) -> отчитывать нечего.
sent.clear()
og._track_kanban_completion(
    tool_name="kanban_complete",
    args={"summary": "не должно быть отправлено"},
    result='{"error": "unknown task_id"}',
    task_id="sess-3",
)
og._maybe_auto_report(session_id="sess-3", task_id="")
check("[страховка] не шлёт отчёт при ошибке kanban_complete", len(sent), 0)

# session_id и task_id разошлись между хуками -> ключ должен совпасть всё равно.
sent.clear()
og._track_kanban_completion(
    tool_name="kanban_complete",
    args={"summary": "ключ по task_id"},
    result="{}",
    task_id="t_shared",
    session_id="",
)
og._maybe_auto_report(session_id="", task_id="t_shared")
check("[страховка] ключ совпадает при task_id из обоих хуков", len(sent), 1)

# ---------------------------------------------------------------------------
# 8. office_report сам: сигнатура (args: dict, **kw), НЕ (text=..., **kwargs)
# ---------------------------------------------------------------------------
#
# Hermes зовёт обработчики как entry.handler(args, **kwargs) — весь словарь
# ОДНИМ позиционным параметром (tools/registry.py). Живой баг: раньше здесь
# было def office_report(text="", **kwargs) — питон биндил первым позиционным
# параметром весь ``args`` целиком на имя ``text``, и внутри функции ``text``
# оказывался словарём, а не строкой (пустой словарь {} даже маскировался под
# «просто пустой текст» — ложно похоже на рабочее поведение). Этот тест ловит
# именно вызов в реальной калling convention Hermes, а не то, как удобнее
# написать самому.

sent.clear()
result = og.office_report({"text": "Миша, от секретаря пришёл вопрос — вот ответ."})
check("[office_report] реальная calling convention отправляет текст строкой", sent, [
    "Миша, от секретаря пришёл вопрос — вот ответ."
])
check("[office_report] возвращает подтверждение", result, "Отправлено в общий чат.")

# Пустой args (модель забыла text или его вообще не было в схеме) -> понятная
# ошибка, а не падение с AttributeError на словаре без .strip().
sent.clear()
result = og.office_report({})
check("[office_report] пустой args -> понятная ошибка, не крэш", result, "Ошибка: пустой текст сообщения.")

# ---------------------------------------------------------------------------

if failures:
    print(f"ПРОВАЛЕНО — {len(failures)} проверок:\n")
    for f in failures:
        print("  •", f)
    sys.exit(1)
print("OK — все проверки прошли")
