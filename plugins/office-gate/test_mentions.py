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

if failures:
    print(f"ПРОВАЛЕНО — {len(failures)} проверок:\n")
    for f in failures:
        print("  •", f)
    sys.exit(1)
print("OK — все проверки прошли")
