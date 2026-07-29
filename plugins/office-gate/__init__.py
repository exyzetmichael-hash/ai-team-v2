"""office-gate v3 — маршрутизация сообщений в общем офис-чате.

ЗАЧЕМ ПЕРЕПИСАНО (v2 -> v3). В v2 каждый агент решал за себя: «моя это тема
или нет?» — семь независимых вызовов модели на одно сообщение. На живом
прогоне это провалилось в обе стороны сразу:

  * при недоступном классификаторе хук возвращал None («пропустить»), и на
    сообщение отвечали ВСЕ СЕМЬ агентов — включая «все закройте ебальники»;
  * каждый из семи, решив «не моя тема», заводил СВОЮ карточку Kanban одному
    и тому же исполнителю — пять дублей финансисту, четыре мозгу, каждая
    карточка это отдельный платный запуск агента.

Корень не в качестве классификатора, а в архитектуре: при независимом решении
семи процессов «отвечает ровно один» недостижимо в принципе. Поэтому v3 —
арбитр: решение принимается ОДИН раз на сообщение, остальные ему подчиняются.

ПОРЯДОК ПРОВЕРОК (сверху вниз, первое совпадение выигрывает):

  1. Личка — пропускаем. Там и так пишут конкретно этому агенту.
  2. Реплай на моё же сообщение — пропускаем, разговор продолжается со мной.
  3. Слэш-команда с @упоминанием бота (`/sethome@legalllmbot`) — обрабатывает
     только названный бот. Без этого правила на одну служебную команду
     отвечали все семеро (поймано на живом прогоне).
  4. Названо МОЁ имя — пропускаем. Без модели, детерминированно.
  5. Названо ЧУЖОЕ имя (а моё — нет) — молчим. Без модели.
  6. Имени нет — идём к арбитру (см. ниже).

АРБИТР (пункт 6). Все семь процессов видят одно и то же сообщение. Они
атомарно борются за право решить: `INSERT OR IGNORE` в общую SQLite-таблицу
`~/.hermes/office-routing.db`. Победитель делает ОДИН вызов модели, показав ей
СРАЗУ ВЕСЬ состав команды с описаниями ролей, и просит выбрать ровно одного
исполнителя. Решение пишется в ту же строку. Проигравшие ждут появления
решения (с таймаутом) и отвечают, только если названы.

Почему это лучше семи независимых «да/нет»:
  * ровно один отвечающий гарантирован механикой, а не удачей;
  * один вызов модели на сообщение вместо семи — в семь раз дешевле;
  * модель выбирает ИЗ СПИСКА, а не отвечает «подходит ли мне» вслепую, не
    зная, кто ещё есть в команде. Это принципиально более сильная постановка.

ОТКАЗОУСТОЙЧИВОСТЬ. Если что угодно ломается (нет ключа, модель недоступна,
база залочена, таймаут ожидания) — маршрут назначается дежурному профилю
(`OFFICE_DEFAULT_RESPONDER`, по умолчанию `secretary`). Это сознательная смена
политики v2: раньше сбой означал «отвечают все» (шум и деньги), теперь —
«отвечает дежурный» (один ответ, и он умеет передать работу дальше). Молчания
в обоих случаях не возникает: на сообщение всегда кто-то отвечает.

ИНСТРУМЕНТ office_report. У агентов Hermes намеренно НЕТ инструмента отправки
сообщений в мессенджер (см. комментарий в toolsets.py: outbound messaging
живёт вне цикла агента). Из-за этого исполнитель карточки физически не мог
сам отчитаться в чат — за него это делал встроенный нотификатор, и отчёт
приходил ботом-диспетчером сухим системным текстом. Плагин регистрирует
`office_report`, который шлёт сообщение в офисный чат ботом САМОГО исполнителя
— чтобы в группе было видно живую передачу работы: кто получил, от кого, что
сделал.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import httpx
except Exception:  # pragma: no cover
    httpx = None

_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

# Русские основы слов, по которым Михаил зовёт каждую роль. Сверка идёт по
# началу слова, поэтому склонения ловятся автоматически ("секретар" ->
# секретарь/секретаря/секретарю/секретарём). Английское имя профиля
# добавляется к его списку автоматически, отдельно писать не нужно.
#
# Переопределить можно через HERMES_OFFICE_GATE_ALIASES с JSON вида
# {"secretary": ["секретар", "сек"], ...} — тогда встроенная карта игнорируется.
ROLE_ALIASES: dict[str, list[str]] = {
    "secretary": ["секретар"],
    "brain": ["мозг", "брейн"],
    "finance": ["финансист", "финанс"],
    "tutor": ["тьютор", "репетитор", "преподават"],
    "tracker": ["трекер", "трэкер"],
    "research": ["ресёрчер", "ресерчер", "ресёрч", "ресерч", "исследоват"],
    "legal": ["юрист", "юрид", "правовед"],
}

# Сколько ждать чужого решения, прежде чем считать арбитраж сорванным.
# Хуки Hermes вызываются синхронно (invoke_hook в hermes_cli/plugins.py —
# обычная функция), поэтому ожидание блокирует цикл СВОЕГО gateway. Держим
# короткий бюджет: лучше разойтись по фолбэку, чем подвесить бота.
_WAIT_TIMEOUT_S = 6.0
_WAIT_POLL_S = 0.1
# Строки старше этого возраста считаются мусором и переигрываются заново —
# защита от «залипшего» арбитра, который умер, не записав решение.
_STALE_CLAIM_S = 30

_cache: dict = {"aliases": None, "roster": None, "roster_at": 0.0}
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Профиль и состав команды
# ---------------------------------------------------------------------------

def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))


def _profile_name() -> str:
    """Имя текущего профиля из пути HERMES_HOME (.../profiles/<name>)."""
    home = _hermes_home()
    return home.name if home.parent.name == "profiles" else "default"


def _default_responder() -> str:
    return os.environ.get("OFFICE_DEFAULT_RESPONDER", "secretary").strip() or "secretary"


def _aliases() -> dict[str, list[str]]:
    """Карта {профиль: [основы слов]}, с добавленным именем самого профиля."""
    with _lock:
        if _cache["aliases"] is not None:
            return _cache["aliases"]
    raw = os.environ.get("HERMES_OFFICE_GATE_ALIASES", "").strip()
    table = dict(ROLE_ALIASES)
    if raw:
        try:
            override = json.loads(raw)
            if isinstance(override, dict):
                table = {str(k): [str(x).lower() for x in v] for k, v in override.items()}
        except Exception as exc:
            logger.warning("office-gate: bad HERMES_OFFICE_GATE_ALIASES (%s), using defaults", exc)
    for name in list(table):
        if name.lower() not in table[name]:
            table[name] = table[name] + [name.lower()]
    with _lock:
        _cache["aliases"] = table
    return table


def _roster() -> dict[str, str]:
    """{профиль: описание роли} по всем профилям команды.

    Источник — `<профиль>/profile.yaml`, ключ `description`: тот же файл, по
    которому Kanban раздаёт задачи (пишется через `hermes profile describe`).
    Одна формулировка на оба механизма, чтобы они не разъезжались.

    Читаем соседние профили, а не только свой: арбитру нужен ВЕСЬ состав, иначе
    он не сможет выбрать «одного из семи» — ровно та слепота, из-за которой
    провалилась v2.
    """
    now = time.time()
    with _lock:
        if _cache["roster"] is not None and now - _cache["roster_at"] < 300:
            return _cache["roster"]

    result: dict[str, str] = {}
    profiles_dir = _hermes_home().parent
    known = set(_aliases())
    try:
        import yaml
        for entry in sorted(profiles_dir.iterdir()):
            if not entry.is_dir() or entry.name not in known:
                continue
            meta = entry / "profile.yaml"
            if not meta.is_file():
                continue
            data = yaml.safe_load(meta.read_text(encoding="utf-8")) or {}
            desc = str((data or {}).get("description") or "").strip()
            if desc:
                result[entry.name] = desc
    except Exception as exc:
        logger.warning("office-gate: could not build roster: %s", exc)

    with _lock:
        _cache["roster"] = result
        _cache["roster_at"] = now
    return result


# ---------------------------------------------------------------------------
# Разбор адресации
# ---------------------------------------------------------------------------

def _mentions(text_lower: str, stems: list[str]) -> bool:
    """True, если к агенту обратились по имени/роли.

    Правило: основа стоит в НАЧАЛЕ слова и после неё не больше трёх букв.
    Ограничение на хвост важно — русские падежные окончания короткие
    («секретар|ю», «мозг|ом», «юрист|а», «секретар|ями» — максимум три буквы),
    а прилагательные и производные длиннее. Без него основа «мозг» ловилась в
    «мозговой штурм», и агент считал, что звали его (поймано тестом).
    """
    for stem in stems:
        if not stem:
            continue
        if re.search(r"(?<!\w)" + re.escape(stem) + r"\w{0,3}(?!\w)", text_lower):
            return True
    return False


def _slash_command_target(text: str) -> Optional[str]:
    """Для `/cmd@botusername` вернуть `botusername` (в нижнем регистре).

    Telegram доставляет служебные команды всем ботам группы, даже адресованные
    конкретному. Без этой проверки на одну `/sethome@legalllmbot` реагировали
    все семеро.
    """
    stripped = (text or "").lstrip()
    if not stripped.startswith("/"):
        return None
    head = stripped.split()[0] if stripped.split() else ""
    if "@" not in head:
        return ""  # команда без адресата
    return head.split("@", 1)[1].strip().lower()


# ---------------------------------------------------------------------------
# Общая таблица решений
# ---------------------------------------------------------------------------

def _routing_db_path() -> Path:
    """Общая для всех профилей база решений.

    Кладём рядом с общей доской Kanban (`~/.hermes/`), а НЕ внутрь профиля:
    смысл таблицы в том, что все семь процессов видят одни и те же строки.
    Это отдельный файл, а не kanban.db — чужую схему не трогаем.
    """
    override = os.environ.get("OFFICE_ROUTING_DB", "").strip()
    if override:
        return Path(override).expanduser()
    return Path(os.path.expanduser("~/.hermes")) / "office-routing.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_routing_db_path()), timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS routes (
            msg_key    TEXT PRIMARY KEY,
            decider    TEXT NOT NULL,
            assignee   TEXT,
            claimed_at REAL NOT NULL,
            decided_at REAL
        )
        """
    )
    return conn


def _msg_key(event, source) -> str:
    platform = getattr(getattr(source, "platform", None), "value", "") or "?"
    chat = str(getattr(source, "chat_id", "") or "?")
    thread = str(getattr(source, "thread_id", "") or "")
    mid = str(getattr(event, "message_id", "") or "")
    if not mid:
        # Крайний случай: адаптер не дал id. Привязываемся к тексту и секунде —
        # семь процессов видят одно и то же сообщение в один и тот же момент,
        # так что ключ у них совпадёт.
        mid = f"t{int(time.time())}:{hash((getattr(event, 'text', '') or '')) & 0xffffff}"
    return f"{platform}:{chat}:{thread}:{mid}"


def _claim_or_wait(key: str, me: str, decide) -> Optional[str]:
    """Вернуть выбранного исполнителя: решить самому или дождаться чужого решения.

    `decide` вызывается только победителем гонки — ровно один раз на сообщение.
    """
    conn = _connect()
    try:
        now = time.time()
        # Атомарная заявка. INSERT OR IGNORE + rowcount — победа достаётся тому,
        # чья вставка реально создала строку.
        cur = conn.execute(
            "INSERT OR IGNORE INTO routes (msg_key, decider, assignee, claimed_at) "
            "VALUES (?, ?, NULL, ?)",
            (key, me, now),
        )
        conn.commit()
        won = cur.rowcount == 1

        if not won:
            # Возможно, прошлый арбитр умер, не записав решение. Тогда заявку
            # можно перехватить — иначе сообщение осталось бы без ответа.
            row = conn.execute(
                "SELECT assignee, claimed_at FROM routes WHERE msg_key = ?", (key,)
            ).fetchone()
            if row and row[0] is None and (now - float(row[1])) > _STALE_CLAIM_S:
                cur = conn.execute(
                    "UPDATE routes SET decider = ?, claimed_at = ? "
                    "WHERE msg_key = ? AND assignee IS NULL AND claimed_at = ?",
                    (me, now, key, row[1]),
                )
                conn.commit()
                won = cur.rowcount == 1

        if won:
            # Падение решателя не должно оставлять остальных ждать таймаут:
            # записываем дежурного как решение, и шестеро узнают об этом сразу.
            try:
                assignee = decide() or _default_responder()
            except Exception as exc:
                logger.warning("office-gate: decision failed (%s), assigning default", exc)
                assignee = _default_responder()
            conn.execute(
                "UPDATE routes SET assignee = ?, decided_at = ? WHERE msg_key = ?",
                (assignee, time.time(), key),
            )
            conn.commit()
            return assignee

        # Проигравший: ждём решения победителя.
        deadline = time.time() + _WAIT_TIMEOUT_S
        while time.time() < deadline:
            row = conn.execute(
                "SELECT assignee FROM routes WHERE msg_key = ?", (key,)
            ).fetchone()
            if row and row[0]:
                return str(row[0])
            time.sleep(_WAIT_POLL_S)
        logger.warning("office-gate: timed out waiting for routing decision on %s", key)
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Решение арбитра
# ---------------------------------------------------------------------------

def _pick_assignee(message_text: str) -> Optional[str]:
    """Один вызов модели: выбрать ровно одного исполнителя из всего состава."""
    roster = _roster()
    if not roster:
        return None
    if httpx is None:
        return None
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        logger.warning("office-gate: no OPENROUTER_API_KEY, falling back to default responder")
        return None

    model = os.environ.get("HERMES_OFFICE_GATE_MODEL", "deepseek/deepseek-v4-flash")
    lines = "\n".join(f"- {name}: {desc}" for name, desc in sorted(roster.items()))
    prompt = (
        "Ты маршрутизатор сообщений в рабочем чате команды. Состав команды:\n"
        f"{lines}\n\n"
        f"Сообщение пользователя: {message_text[:800]}\n\n"
        "Кто из команды должен ответить? Выбери РОВНО ОДНОГО — того, чья зона "
        "ближе всего. Если сообщение ни к чьей зоне не относится (болтовня, "
        "эмоции, общий вопрос) — выбери "
        f"{_default_responder()}.\n"
        "Ответь ТОЛЬКО именем профиля из списка, одним словом, без пояснений."
    )
    try:
        resp = httpx.post(
            _CHAT_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 10,
                "temperature": 0,
            },
            timeout=8.0,
        )
        resp.raise_for_status()
        answer = resp.json()["choices"][0]["message"]["content"].strip().lower()
    except Exception as exc:
        logger.warning("office-gate: routing call failed (%s), using default responder", exc)
        return None

    # Модель может ответить с пунктуацией или лишним словом — ищем известное имя.
    for name in roster:
        if re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", answer):
            return name
    logger.warning("office-gate: unrecognized routing answer %r, using default responder", answer)
    return None


# ---------------------------------------------------------------------------
# Хук
# ---------------------------------------------------------------------------

def decide_route(text: str, me: str, table: dict[str, list[str]],
                 bot_username: str) -> Optional[str]:
    """Детерминированная часть маршрутизации (без сети и без базы).

    Возвращает: имя профиля, если адресат определён однозначно; "" если
    сообщение адресовано не мне (молчать); None — нужен арбитр.
    Вынесено отдельной функцией, чтобы покрываться тестами без gateway.
    """
    text_lower = (text or "").lower()

    # Слэш-команда с адресом: отвечает только названный бот.
    target = _slash_command_target(text)
    if target:
        return me if bot_username and target == bot_username.lower() else ""

    # Позвали меня по имени.
    if _mentions(text_lower, table.get(me, [me.lower()])):
        return me

    # Позвали кого-то другого, а меня — нет.
    for other, stems in table.items():
        if other == me:
            continue
        if _mentions(text_lower, stems):
            return ""

    return None


def office_gate(event, gateway, session_store, **kwargs):
    try:
        source = getattr(event, "source", None)
        # 1. Личка — не трогаем
        if source is None or getattr(source, "chat_type", "dm") == "dm":
            return None

        # 2. Отвечают на моё же сообщение — разговор продолжается со мной
        if getattr(event, "reply_to_is_own_message", False):
            return None

        text = getattr(event, "text", "") or ""
        me = _profile_name()
        table = _aliases()
        bot_username = os.environ.get("OFFICE_BOT_USERNAME", "").strip()

        # 3-5. Детерминированная адресация, без модели и без базы
        decided = decide_route(text, me, table, bot_username)
        if decided == me:
            return None
        if decided == "":
            return {"action": "skip", "reason": "office-gate: addressed to someone else"}

        # 6. Имени нет — арбитр решает один раз на всех
        key = _msg_key(event, source)
        assignee = _claim_or_wait(key, me, lambda: _pick_assignee(text))
        if assignee is None:
            # Решения не дождались. Отвечает дежурный — чтобы сообщение не
            # осталось без ответа и при этом не ответили все разом.
            assignee = _default_responder()
        if assignee == me:
            return None
        return {"action": "skip", "reason": f"office-gate: routed to '{assignee}'"}

    except Exception as exc:  # хук не должен ронять доставку сообщения
        logger.warning("office-gate: unexpected error (%s); deferring to default responder", exc)
        try:
            if _profile_name() == _default_responder():
                return None
        except Exception:
            return None
        return {"action": "skip", "reason": "office-gate: error, deferred to default responder"}


# ---------------------------------------------------------------------------
# Инструмент office_report — отчёт исполнителя своим голосом
# ---------------------------------------------------------------------------

OFFICE_REPORT_SCHEMA = {
    "name": "office_report",
    "description": (
        "Отчитаться о выполненной задаче с доски Kanban в СВОЁМ топике общего "
        "чата команды, от своего имени. Скажи, от кого была задача, в чём она "
        "состояла, и дай результат — обычным человеческим текстом, как коллега. "
        "Топик выбирается автоматически (всегда твой), адресата указывать нельзя. "
        "Для обычного разговора инструмент не нужен: на сообщение Михаила просто "
        "отвечай там, где он спросил."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "Текст сообщения в общий чат, своими словами.",
            },
        },
        "required": ["text"],
    },
}


def office_report(text: str = "", **kwargs) -> str:
    """Отправить отчёт в СВОЙ топик офисного чата ботом ЭТОГО профиля.

    Адрес жёстко задан окружением профиля (`OFFICE_GROUP_CHAT_ID` +
    `OFFICE_GROUP_THREAD_ID`) и не является аргументом: «отчёт всегда в свой
    топик и только в него» — свойство конструкции, а не инструкция модели,
    которую та может забыть. Обсуждение при этом идёт в любом топике как
    обычно: на реплики Михаила агент отвечает штатным путём gateway, туда же,
    где его спросили, — этот инструмент к разговору отношения не имеет.
    """
    text = (text or "").strip()
    if not text:
        return "Ошибка: пустой текст сообщения."
    if httpx is None:
        return "Ошибка: httpx недоступен, отправить не могу."

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("OFFICE_GROUP_CHAT_ID", "").strip()
    if not token:
        return "Ошибка: TELEGRAM_BOT_TOKEN не задан в профиле."
    if not chat_id:
        return "Ошибка: OFFICE_GROUP_CHAT_ID не задан в профиле."

    payload: dict = {"chat_id": chat_id, "text": text[:4000]}
    thread = os.environ.get("OFFICE_GROUP_THREAD_ID", "").strip()
    if thread:
        payload["message_thread_id"] = int(thread) if thread.isdigit() else thread

    try:
        resp = httpx.post(_TELEGRAM_API.format(token=token), json=payload, timeout=15.0)
        resp.raise_for_status()
        return "Отправлено в общий чат."
    except Exception as exc:
        logger.warning("office-gate: office_report failed: %s", exc)
        return f"Не удалось отправить в общий чат: {exc}"


def register(ctx) -> None:
    ctx.register_hook("pre_gateway_dispatch", office_gate)
    try:
        ctx.register_tool(
            name="office_report",
            toolset="office",
            schema=OFFICE_REPORT_SCHEMA,
            handler=office_report,
            description="Написать в общий рабочий чат команды от своего имени",
            emoji="📣",
        )
    except Exception as exc:
        # Регистрация инструмента не должна ломать гейт: маршрутизация важнее.
        logger.warning("office-gate: could not register office_report tool: %s", exc)
