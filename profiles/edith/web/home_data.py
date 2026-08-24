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

TODOIST_API = "https://api.todoist.com/rest/v2/tasks"
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
            raw = json.loads(resp.read().decode("utf-8"))
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
# сборка и кэш
# --------------------------------------------------------------------------

def _build() -> dict:
    return {
        "tasks": fetch_tasks(),
        "events": fetch_today_events(),
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
