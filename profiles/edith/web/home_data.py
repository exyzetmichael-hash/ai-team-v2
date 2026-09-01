#!/usr/bin/env python3
"""Данные для главного экрана: задачи и расписание на сегодня.

Отдельно от server.py, потому что это единственная часть, которая ходит
во внешние сервисы и потому единственная, которая может тормозить. Здесь
же живёт кэш — главный экран обязан открываться мгновенно.

ПОЧЕМУ КЭШ ОБЯЗАТЕЛЕН
---------------------
Михаил про то, что заставляет его бросать инструменты: «медленность и
скука... чем больше действий мне приходится делать, тем меньше у меня
желания делать что-то». Ходить в Todoist и Google Calendar на каждое
открытие — это 300-800 мс ожидания на экране, который должен появляться
мгновенно. Поэтому отдаём последнее известное состояние сразу, а свежее
подтягиваем фоном.

ПОЧЕМУ БЕЗ LLM
--------------
Ни одного вызова модели: это экран, который открывается по десять раз в
день. Задачи берём напрямую из Todoist REST, расписание — из Google
Calendar. Приветствие собирается на клиенте по часам телефона (см.
app.js): сервер в CEST, профиль в Красноярске, Михаил переезжает в Питер —
единственные часы, которым можно верить, это часы устройства в руке.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

EDITH_HOME = Path(os.environ.get("HERMES_HOME", "") or (Path.home() / ".hermes" / "profiles" / "edith"))
TOKEN_PATH = EDITH_HOME / "google_token.json"
ENV_PATH = EDITH_HOME / ".env"
FINANCE_CONFIG_PATH = EDITH_HOME / "finance_webhook.json"

try:
    from zoneinfo import ZoneInfo
    MONEY_TZ = ZoneInfo("Europe/Moscow")
except Exception:  # pragma: no cover — на всякий случай, если tzdata нет
    MONEY_TZ = None

# ⚠️ 2026-08-24: Todoist убрал REST v2 (410 Gone на /rest/v2/*), новый
# эндпоинт /api/v1/* — унифицированный с Sync API, и ответ там ОБЁРНУТ в
# {"results": [...], "next_cursor": ...}, а не голый массив, как раньше.
# Живьём поймано: без этого код падал в `except: return []` молча — экран
# показывал бы «задач нет» вместо реальной ошибки, ту же грабли, что уже
# однажды сработала с фильтром «назначено мне» (см. докстринг ниже).
TODOIST_API = "https://api.todoist.com/api/v1/tasks"
HTTP_TIMEOUT = 12

# Свежесть кэша. 90 секунд — компромисс: задачу, добавленную в телефоне
# минуту назад, увидим почти сразу, но десять открытий подряд не превратятся
# в десять походов по сети.
CACHE_TTL = 90

_cache: dict[str, Any] = {"data": None, "at": 0.0}
_cache_lock = threading.Lock()
_refresh_lock = threading.Lock()


def read_env(name: str) -> str:
    """Читает ключ из .env профиля.

    Не через os.environ: сервис может быть запущен systemd-юнитом без
    EnvironmentFile, и тогда переменных там просто нет. Файл — источник
    правды, тот же, что у самого гейтвея.
    """
    try:
        text = ENV_PATH.read_text(encoding="utf-8")
    except Exception:
        return ""
    m = re.search(rf"^{re.escape(name)}=(.*)$", text, re.M)
    return m.group(1).strip().strip("\"'") if m else ""


# --------------------------------------------------------------------------
# Todoist
# --------------------------------------------------------------------------

def fetch_tasks() -> list[dict]:
    """Задачи на сегодня и просроченные.

    ⚠️ Фильтр намеренно БЕЗ «назначено мне»: аккаунт личный, задачи Михаила
    без назначения, и такой фильтр отдаёт пустоту. Эта грабля уже сработала
    один раз — EDITH сказала «задач нет», когда их было полно (зафиксировано
    в SOUL.md и MORNING.md).
    """
    key = read_env("TODOIST_API_KEY")
    if not key:
        return []
    url = TODOIST_API + "?" + urllib.parse.urlencode({"filter": "today | overdue"})
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = json.loads(resp.read().decode("utf-8")).get("results", [])
    except Exception as exc:
        print(f"[веб] Todoist недоступен: {exc}", file=sys.stderr)
        return []

    today = dt.date.today().isoformat()
    tasks = []
    for t in raw:
        due = (t.get("due") or {}).get("date") or ""
        tasks.append({
            "id": t.get("id"),
            "content": t.get("content") or "",
            "due": due,
            "overdue": bool(due and due[:10] < today),
            "priority": t.get("priority") or 1,  # 4 = максимальный в API
            "url": t.get("url") or "",
        })
    # Просроченные вперёд, дальше по приоритету: сверху то, что горит.
    tasks.sort(key=lambda t: (not t["overdue"], -t["priority"], t["due"]))
    return tasks[:12]


# --------------------------------------------------------------------------
# Google Calendar
# --------------------------------------------------------------------------

def fetch_today_events() -> list[dict]:
    """События на сегодня со ВСЕХ календарей, включая «Политех — расписание».

    Берём из всех, а не только из primary: пары лежат в отдельном календаре
    (см. docs/politeh-schedule.md), и главный экран без них бесполезен
    ровно в учебные дни.
    """
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except ImportError:
        return []
    if not TOKEN_PATH.exists():
        return []

    try:
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH))
        if not creds.valid:
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
            else:
                return []
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)

        # Окно — сутки по локальному времени сервера. Возможный сдвиг на
        # час-другой относительно Питера не критичен: события всё равно
        # отдаются с их собственными метками времени и отображаются по
        # часам телефона.
        now = dt.datetime.now(dt.timezone.utc)
        start = (now - dt.timedelta(hours=6)).isoformat()
        end = (now + dt.timedelta(hours=30)).isoformat()

        events: list[dict] = []
        cal_list = service.calendarList().list().execute()
        for cal in cal_list.get("items", []):
            resp = service.events().list(
                calendarId=cal["id"], timeMin=start, timeMax=end,
                singleEvents=True, orderBy="startTime", maxResults=30,
            ).execute()
            for ev in resp.get("items", []):
                start_raw = (ev.get("start") or {})
                when = start_raw.get("dateTime") or start_raw.get("date") or ""
                if not when:
                    continue
                events.append({
                    "summary": ev.get("summary") or "(без названия)",
                    "start": when,
                    "location": ev.get("location") or "",
                    "all_day": "date" in start_raw and "dateTime" not in start_raw,
                })
        events.sort(key=lambda e: e["start"])
        return events[:12]
    except Exception as exc:
        print(f"[веб] Calendar недоступен: {exc}", file=sys.stderr)
        return []


# --------------------------------------------------------------------------
# Деньги и почта (главный экран, 2026-09-01)
# --------------------------------------------------------------------------

def _google_service(name: str, version: str):
    """Общий билдер для Sheets/Gmail — тот же токен, что и у Calendar.

    ⚠️ Токен изначально выпущен узким (`--services calendar`, см.
    docs/phase-2-runbook.md — сознательное решение «без лишних разрешений»).
    Если Sheets/Gmail отсюда падают с 403 — это не баг, а токену не хватает
    scope. Чинится не руками, а тем же диалоговым способом, каким заводился
    Calendar: попросить EDITH расширить доступ (Sheets read + Gmail read),
    см. docs/web-ui.md. До тех пор соответствующий блок на экране просто не
    показывается — ровно тот же принцип, что и у пустых «Задач»/«Сегодня».
    """
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    if not TOKEN_PATH.exists():
        return None
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH))
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
        else:
            return None
    return build(name, version, credentials=creds, cache_discovery=False)


def _spreadsheet_id() -> str:
    """ID финансовой таблицы — берём из конфига вебхука, не дублируем.

    Вебхук сейчас не в фокусе (docs/weekly-statements.md), но файл с его
    конфигом остаётся источником правды для id таблицы: заводить второй
    такой же конфиг только ради главного экрана незачем.
    """
    try:
        cfg = json.loads(FINANCE_CONFIG_PATH.read_text(encoding="utf-8"))
        return cfg.get("spreadsheet_id") or ""
    except Exception:
        return ""


def _parse_amount(raw: str) -> Optional[float]:
    try:
        return float(str(raw).replace("\xa0", "").replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return None


def fetch_money() -> Optional[dict]:
    """Траты этого месяца + категории у лимита. None — таблица недоступна.

    Месяц считается в Europe/Moscow (тот же принцип, что у budget_watchdog.py
    и finance_webhook.py — деньги никогда не считаются по времени сервера).
    """
    sheet_id = _spreadsheet_id()
    if not sheet_id or MONEY_TZ is None:
        return None
    try:
        service = _google_service("sheets", "v4")
        if service is None:
            return None
        values = service.spreadsheets().values()
        ops = values.get(spreadsheetId=sheet_id, range="Операции!A2:F").execute().get("values", [])
        limits = values.get(spreadsheetId=sheet_id, range="Лимиты!A2:D").execute().get("values", [])
    except Exception as exc:
        print(f"[веб] финансовая таблица недоступна: {exc}", file=sys.stderr)
        return None

    today = dt.datetime.now(MONEY_TZ).date()
    month_start = today.replace(day=1)

    spent_by_cat: dict[str, float] = {}
    spent_month = 0.0
    income_month = 0.0
    for row in ops:
        if len(row) < 4:
            continue
        date_raw, typ, cat = row[0], row[1], row[2]
        amount = _parse_amount(row[3])
        if amount is None:
            continue
        try:
            date = dt.date.fromisoformat(str(date_raw)[:10])
        except ValueError:
            continue
        if not (month_start <= date <= today):
            continue
        if typ == "Расход":
            spent_month += amount
            spent_by_cat[cat] = spent_by_cat.get(cat, 0.0) + amount
        elif typ == "Доход":
            income_month += amount

    over_limit: list[str] = []
    near_limit: list[str] = []
    for row in limits:
        if len(row) < 4:
            continue
        cat = row[0]
        limit_month = _parse_amount(row[3]) or 0.0
        if limit_month <= 0:
            continue
        ratio = spent_by_cat.get(cat, 0.0) / limit_month
        if ratio >= 1.0:
            over_limit.append(cat)
        elif ratio >= 0.9:
            near_limit.append(cat)

    return {
        "spent_month": round(spent_month),
        "income_month": round(income_month),
        "over_limit": over_limit,
        "near_limit": near_limit,
    }


def fetch_mail() -> Optional[dict]:
    """Число непрочитанных во входящих. None — Gmail недоступен."""
    try:
        service = _google_service("gmail", "v1")
        if service is None:
            return None
        resp = service.users().messages().list(
            userId="me", q="is:unread in:inbox", maxResults=1,
        ).execute()
        return {"unread": resp.get("resultSizeEstimate", 0)}
    except Exception as exc:
        print(f"[веб] Gmail недоступен (возможно не хватает scope): {exc}", file=sys.stderr)
        return None


# --------------------------------------------------------------------------
# сборка и кэш
# --------------------------------------------------------------------------

def _build() -> dict:
    return {
        "tasks": fetch_tasks(),
        "events": fetch_today_events(),
        "money": fetch_money(),
        "mail": fetch_mail(),
        "fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
    }


def get_home(force: bool = False) -> dict:
    """Данные главного экрана. Свежие из кэша, устаревшие — тоже из кэша.

    Ключевое: устаревший кэш возвращается НЕМЕДЛЕННО, а обновление уходит
    в фон. Экран не ждёт сеть никогда, кроме самого первого запуска за всё
    время жизни сервиса.
    """
    with _cache_lock:
        data, at = _cache["data"], _cache["at"]

    fresh = data is not None and (time.time() - at) < CACHE_TTL
    if fresh and not force:
        return data

    if data is None:
        # Первый запуск: ждать нечего, кэша нет вообще.
        built = _build()
        with _cache_lock:
            _cache["data"], _cache["at"] = built, time.time()
        return built

    _refresh_in_background()
    return data


def _refresh_in_background() -> None:
    # Один поток обновления за раз: десять быстрых открытий подряд не должны
    # породить десять параллельных походов в Todoist.
    if not _refresh_lock.acquire(blocking=False):
        return

    def worker() -> None:
        try:
            built = _build()
            with _cache_lock:
                _cache["data"], _cache["at"] = built, time.time()
        except Exception as exc:
            print(f"[веб] фоновое обновление главного экрана упало: {exc}", file=sys.stderr)
        finally:
            _refresh_lock.release()

    threading.Thread(target=worker, daemon=True).start()


def warm_cache() -> None:
    """Прогреть кэш при старте сервиса, чтобы первое открытие было быстрым."""
    threading.Thread(target=lambda: get_home(force=True), daemon=True).start()


# --------------------------------------------------------------------------
# распознавание речи
# --------------------------------------------------------------------------

GROQ_STT_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODEL = "whisper-large-v3-turbo"


def transcribe(audio: bytes, filename: str = "voice.webm") -> tuple[Optional[str], Optional[str]]:
    """Аудио → текст через Groq Whisper. Возвращает (текст, ошибка).

    Почему Groq, а не распознавание в самом браузере: браузерный Web Speech
    API отправляет звук в Google, по-русски распознаёт заметно хуже и в
    Firefox отсутствует вовсе. Groq у Михаила уже подключён как STT для
    голосовых в Telegram — тот же ключ, ничего нового заводить не надо.

    Текст попадает в поле ввода, а не отправляется сразу: распознавание
    иногда врёт, и увидеть это ДО отправки дешевле, чем получить ответ не
    на тот вопрос.
    """
    key = read_env("GROQ_API_KEY")
    if not key:
        return None, "GROQ_API_KEY не найден в .env профиля"

    boundary = "----edithvoice" + os.urandom(8).hex()
    parts: list[bytes] = []

    def field(name: str, value: str) -> None:
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode("utf-8")
        )

    field("model", GROQ_MODEL)
    field("language", "ru")
    field("response_format", "json")
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
        f"Content-Type: application/octet-stream\r\n\r\n".encode("utf-8")
    )
    parts.append(audio)
    parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(parts)

    req = urllib.request.Request(
        GROQ_STT_URL, data=body,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return (data.get("text") or "").strip(), None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        print(f"[веб] Groq вернул {exc.code}: {detail}", file=sys.stderr)
        return None, f"распознавание не удалось (HTTP {exc.code})"
    except Exception as exc:
        print(f"[веб] Groq недоступен: {exc}", file=sys.stderr)
        return None, "распознавание не удалось"
