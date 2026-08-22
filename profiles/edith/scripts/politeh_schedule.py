#!/usr/bin/env python3
"""Синхронизация расписания Политеха (ruz.spbstu.ru) в Google Calendar.

НЕ агент: ни одного вызова LLM. Запускается кроном в режиме
`--script --no-agent`, как сторож почты (см. mail_watchdog.py) — пустой
stdout означает полную тишину и ноль стоимости, непустой уходит в Telegram
как есть.

ЗАЧЕМ ОТДЕЛЬНЫЙ КАЛЕНДАРЬ, А НЕ primary
--------------------------------------
Все события пишутся в отдельный календарь «Политех — расписание», который
скрипт заводит сам при первом запуске. Это не косметика: синхронизация
УДАЛЯЕТ события, которых больше нет в источнике, и любая ошибка в матчинге
на primary означала бы снос настоящих встреч Михаила. В отдельном календаре
худший сценарий — покоцанное расписание, которое чинится одним прогоном.
Дополнительная страховка: скрипт трогает только события со своей меткой в
extendedProperties и только внутри своего окна дат.

ЧАСОВОЙ ПОЯС
------------
Пары идут по Питеру, а профиль EDITH до сих пор в Asia/Krasnoyarsk (переезд
конца августа 2026). Поэтому пояс событий задаётся ЯВНО (EVENT_TIMEZONE),
а не берётся из системного времени сервера — сервер вообще в CEST. Три
разных пояса в одной задаче: перепутать здесь легко, и результат — пары в
календаре не в то время.

ПЕРВЫЙ ЗАПУСК
-------------
  # 1. найти свою группу (id понадобится в конфиге)
  python3 politeh_schedule.py --find-group 3530904/90001

  # 2. посмотреть, что получится, НИЧЕГО не записывая
  python3 politeh_schedule.py --group-id 12345 --dry-run

  # 3. когда вывод выглядит правильно — записать конфиг и синхронизировать
  echo '{"group_id": 12345}' > ~/.hermes/profiles/edith/politeh_schedule.json
  python3 politeh_schedule.py

⚠️ Схему ответа ruz-API живьём проверить при написании не удалось (API
недоступен из среды разработки), поэтому парсинг намеренно защитный: любое
неожиданное поле даёт пропуск пары с жалобой в stderr, а не падение. Первый
прогон обязательно делай через --dry-run и сверь глазами с сайтом.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

EDITH_HOME = Path(os.environ.get("HERMES_HOME", "") or (Path.home() / ".hermes" / "profiles" / "edith"))
TOKEN_PATH = EDITH_HOME / "google_token.json"
CONFIG_PATH = EDITH_HOME / "politeh_schedule.json"
STATE_PATH = EDITH_HOME / "politeh_schedule_state.json"

RUZ_BASE = "https://ruz.spbstu.ru/api/v1/ruz"
HTTP_TIMEOUT = 30

CALENDAR_NAME = "Политех — расписание"
EVENT_TIMEZONE = "Europe/Moscow"  # пары идут по Питеру, см. докстринг
# Метка, по которой скрипт узнаёт СВОИ события. Всё, что без неё, не трогаем
# никогда — даже внутри своего календаря, даже если он кажется пустым.
MARKER_KEY = "politeh_sync"
MARKER_VALUE = "v1"

DEFAULT_WEEKS_AHEAD = 2


# --------------------------------------------------------------------------
# ruz.spbstu.ru
# --------------------------------------------------------------------------

def _ruz_get(path: str, params: Optional[dict] = None) -> Any:
    url = f"{RUZ_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        # Без внятного User-Agent ruz иногда отвечает пустотой.
        "User-Agent": "Mozilla/5.0 (compatible; edith-schedule-sync/1.0)",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def find_group(query: str) -> None:
    """Печатает найденные группы с их id — для первичной настройки."""
    try:
        data = _ruz_get("/search/groups", {"q": query})
    except Exception as exc:
        print(f"Не смог найти группу: {exc}", file=sys.stderr)
        sys.exit(1)
    groups = data.get("groups") if isinstance(data, dict) else data
    if not groups:
        print(f"По запросу «{query}» групп не нашлось.")
        return
    for g in groups:
        print(f"id={g.get('id')}  {g.get('name')}  ({g.get('faculty', {}).get('name', '?')}, {g.get('level', '?')} уровень)")


def fetch_week(group_id: int, monday: dt.date) -> list[dict]:
    """Возвращает список пар за неделю, начинающуюся с monday.

    Каждая пара — плоский dict с тем, что нужно календарю. Всё, что не
    распарсилось, пропускается с жалобой в stderr: одна кривая пара не
    должна ронять синхронизацию всей недели.
    """
    try:
        data = _ruz_get(f"/scheduler/{group_id}", {"date": monday.isoformat()})
    except urllib.error.HTTPError as exc:
        print(f"ruz вернул HTTP {exc.code} за неделю {monday}", file=sys.stderr)
        return []
    except Exception as exc:
        print(f"Не смог получить расписание за неделю {monday}: {exc}", file=sys.stderr)
        return []

    if isinstance(data, dict) and data.get("error"):
        print(f"ruz вернул ошибку за неделю {monday}: {data.get('text')}", file=sys.stderr)
        return []

    lessons: list[dict] = []
    for day in (data.get("days") or []):
        date_str = day.get("date")
        if not date_str:
            continue
        # В ответе дата может быть с временем — берём только дату.
        date_str = str(date_str)[:10]
        try:
            day_date = dt.date.fromisoformat(date_str)
        except ValueError:
            print(f"Непонятная дата дня: {date_str!r}", file=sys.stderr)
            continue

        for raw in (day.get("lessons") or []):
            parsed = _parse_lesson(raw, day_date)
            if parsed:
                lessons.append(parsed)
    return lessons


_SUBGROUP_RE = re.compile(r"п/?г\.?\s*(\d+)", re.IGNORECASE)


def _subgroup_marker(note: str) -> str:
    """Достаёт номер подгруппы из additional_info, если он там есть.

    Живьём поймано: лабораторная может делиться на «п/г 1» и «п/г 2» —
    два разных занятия в один и тот же час, разные аудитории. Без этого
    маркера у обоих одинаковый lesson_key, и второе тихо затирает первое
    в словаре desired — то есть подгруппа 1 молча пропадала бы из
    календаря. additional_info для не поделённых занятий содержит другой
    текст («Поток» и т.п.) — на это регулярка не реагирует, пустая строка.
    """
    m = _SUBGROUP_RE.search(note or "")
    return f"пг{m.group(1)}" if m else ""


def _parse_lesson(raw: dict, day_date: dt.date) -> Optional[dict]:
    subject = (raw.get("subject") or raw.get("subject_short") or "").strip()
    time_start = raw.get("time_start")
    time_end = raw.get("time_end")
    if not subject or not time_start or not time_end:
        print(f"Пропускаю пару без темы/времени: {raw!r}"[:300], file=sys.stderr)
        return None

    try:
        # time_start приходит как "10:00" или "10:00:00"
        h1, m1 = str(time_start).split(":")[:2]
        h2, m2 = str(time_end).split(":")[:2]
        start = dt.datetime.combine(day_date, dt.time(int(h1), int(m1)))
        end = dt.datetime.combine(day_date, dt.time(int(h2), int(m2)))
    except (ValueError, TypeError):
        print(f"Пропускаю пару с непарсящимся временем: {time_start!r}-{time_end!r}", file=sys.stderr)
        return None

    lesson_type = ((raw.get("typeObj") or raw.get("type_obj") or {}).get("abbr")
                   or (raw.get("typeObj") or raw.get("type_obj") or {}).get("name")
                   or "")

    auditories = []
    for aud in (raw.get("auditories") or []):
        name = (aud.get("name") or "").strip()
        building = ((aud.get("building") or {}).get("name") or "").strip()
        auditories.append(f"{name} ({building})" if building else name)

    teachers = []
    for t in (raw.get("teachers") or []):
        full = (t.get("full_name") or t.get("name") or "").strip()
        if full:
            teachers.append(full)

    note = (raw.get("additional_info") or "").strip()

    return {
        "subject": subject,
        "type": lesson_type,
        "start": start,
        "end": end,
        "location": ", ".join(a for a in auditories if a),
        "teachers": ", ".join(teachers),
        "note": note,
        "subgroup": _subgroup_marker(note),
    }


def lesson_key(lesson: dict) -> str:
    """Стабильный ключ пары — по нему находим ранее созданное событие.

    Намеренно НЕ включает аудиторию и преподавателя: если пару перенесли в
    другой кабинет, это то же занятие, его надо обновить, а не удалить и
    создать заново (иначе слетают напоминания, которые Михаил мог поставить).
    Смена времени — уже другая пара, и это правильно: старую надо убрать.

    Подгруппа (см. `_subgroup_marker`) в ключе — иначе два занятия одной
    группы в один час (например, лаба, поделённая на «п/г 1»/«п/г 2»)
    схлопываются в одно, и одна из подгрупп тихо пропадает из calendar.
    """
    key = f"{lesson['start'].isoformat()}|{lesson['subject']}|{lesson['type']}"
    if lesson.get("subgroup"):
        key += f"|{lesson['subgroup']}"
    return key


def lesson_to_event(lesson: dict) -> dict:
    title = lesson["subject"]
    if lesson["type"]:
        title = f"{title} ({lesson['type']})"
    if lesson.get("subgroup"):
        title = f"{title} [{lesson['subgroup']}]"

    description_parts = []
    if lesson["teachers"]:
        description_parts.append(f"Преподаватель: {lesson['teachers']}")
    if lesson["note"]:
        description_parts.append(lesson["note"])
    description_parts.append("— синхронизировано из ruz.spbstu.ru")

    return {
        "summary": title,
        "location": lesson["location"],
        "description": "\n".join(description_parts),
        "start": {"dateTime": lesson["start"].isoformat(), "timeZone": EVENT_TIMEZONE},
        "end": {"dateTime": lesson["end"].isoformat(), "timeZone": EVENT_TIMEZONE},
        "extendedProperties": {"private": {
            MARKER_KEY: MARKER_VALUE,
            "ruz_key": lesson_key(lesson),
        }},
        # Пары идут по расписанию, отдельные напоминания не нужны — иначе
        # телефон будет звенеть по пять раз в день.
        "reminders": {"useDefault": False},
    }


# --------------------------------------------------------------------------
# Google Calendar
# --------------------------------------------------------------------------

def build_calendar_service():
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except ImportError as exc:
        print(f"[расписание] нет google-библиотек: {exc}. Запускай venv-питоном Hermes.", file=sys.stderr)
        sys.exit(1)

    if not TOKEN_PATH.exists():
        print(f"[расписание] нет токена Google по пути {TOKEN_PATH} — сначала пройди setup скилла google-workspace.", file=sys.stderr)
        sys.exit(1)

    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH))
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
        else:
            print("[расписание] токен Google невалиден и не обновляется — нужна повторная авторизация.", file=sys.stderr)
            sys.exit(1)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def get_or_create_calendar(service) -> str:
    """Находит календарь расписания по имени, создаёт при отсутствии.

    id найденного календаря кэшируем в state — искать перебором каждый
    прогон незачем, а главное, переименуй Михаил календарь руками, скрипт
    продолжит писать в тот же, а не заведёт второй.
    """
    state = _load_state()
    cached = state.get("calendar_id")
    if cached:
        try:
            service.calendars().get(calendarId=cached).execute()
            return cached
        except Exception:
            print("[расписание] закэшированный календарь пропал, ищу заново", file=sys.stderr)

    page_token = None
    while True:
        resp = service.calendarList().list(pageToken=page_token).execute()
        for item in resp.get("items", []):
            if item.get("summary") == CALENDAR_NAME:
                state["calendar_id"] = item["id"]
                _save_state(state)
                return item["id"]
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    created = service.calendars().insert(body={
        "summary": CALENDAR_NAME,
        "description": "Пары СПбПУ, синхронизируется автоматически из ruz.spbstu.ru. Ручные правки перетираются.",
        "timeZone": EVENT_TIMEZONE,
    }).execute()
    state["calendar_id"] = created["id"]
    _save_state(state)
    print(f"[расписание] создан календарь «{CALENDAR_NAME}»", file=sys.stderr)
    return created["id"]


def list_existing(service, calendar_id: str, start: dt.date, end: dt.date) -> dict[str, dict]:
    """Существующие события скрипта в окне, по ruz_key.

    Берём только помеченные своим маркером — всё остальное в этом календаре
    (если Михаил что-то добавил руками) не наше дело.
    """
    out: dict[str, dict] = {}
    page_token = None
    time_min = dt.datetime.combine(start, dt.time.min).isoformat() + "Z"
    time_max = dt.datetime.combine(end, dt.time.max).isoformat() + "Z"
    while True:
        resp = service.events().list(
            calendarId=calendar_id,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            maxResults=2500,
            pageToken=page_token,
            privateExtendedProperty=f"{MARKER_KEY}={MARKER_VALUE}",
        ).execute()
        for ev in resp.get("items", []):
            key = (ev.get("extendedProperties", {}).get("private", {}) or {}).get("ruz_key")
            if key:
                out[key] = ev
        page_token = resp.get("nextPageToken")
        if not page_token:
            return out


def _event_differs(existing: dict, desired: dict) -> bool:
    if existing.get("summary") != desired["summary"]:
        return True
    if (existing.get("location") or "") != (desired["location"] or ""):
        return True
    if (existing.get("description") or "") != desired["description"]:
        return True
    return False


# --------------------------------------------------------------------------
# состояние
# --------------------------------------------------------------------------

def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        print(f"[расписание] не смог сохранить состояние: {exc}", file=sys.stderr)


def _load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def monday_of(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=d.weekday())


def main() -> None:
    parser = argparse.ArgumentParser(description="Синхронизация расписания Политеха в Google Calendar")
    parser.add_argument("--find-group", metavar="ЗАПРОС", help="найти id группы по номеру и выйти")
    parser.add_argument("--group-id", type=int, help="id группы (иначе берётся из politeh_schedule.json)")
    parser.add_argument("--weeks", type=int, help=f"на сколько недель вперёд синхронизировать (по умолчанию {DEFAULT_WEEKS_AHEAD})")
    parser.add_argument("--dry-run", action="store_true", help="показать, что было бы сделано, ничего не записывая")
    args = parser.parse_args()

    if args.find_group:
        find_group(args.find_group)
        return

    config = _load_config()
    group_id = args.group_id or config.get("group_id")
    if not group_id:
        print(f"[расписание] не задан group_id — ни в аргументах, ни в {CONFIG_PATH}. "
              f"Найди свой: politeh_schedule.py --find-group <номер группы>", file=sys.stderr)
        sys.exit(1)
    weeks = args.weeks or config.get("weeks_ahead") or DEFAULT_WEEKS_AHEAD

    today = dt.date.today()
    first_monday = monday_of(today)
    window_start = first_monday
    window_end = first_monday + dt.timedelta(weeks=weeks, days=-1)

    lessons: list[dict] = []
    for i in range(weeks):
        lessons.extend(fetch_week(int(group_id), first_monday + dt.timedelta(weeks=i)))

    if not lessons:
        # Пусто — это нормально в каникулы. Но пусто ТАКЖЕ выглядит падение
        # ruz, а стереть всё расписание из-за недоступного сайта нельзя.
        # Поэтому при пустом ответе ничего не удаляем и молча выходим.
        print("[расписание] источник не вернул ни одной пары — ничего не меняю", file=sys.stderr)
        return

    desired = {lesson_key(l): lesson_to_event(l) for l in lessons}

    if args.dry_run:
        print(f"Нашлось пар: {len(desired)} (недели с {window_start} по {window_end})\n")
        for key in sorted(desired):
            ev = desired[key]
            print(f"  {ev['start']['dateTime']}  {ev['summary']}  [{ev['location'] or 'без аудитории'}]")
        print("\n(--dry-run: в календарь ничего не записано)")
        return

    service = build_calendar_service()
    calendar_id = get_or_create_calendar(service)
    existing = list_existing(service, calendar_id, window_start, window_end)

    created = updated = deleted = 0

    for key, body in desired.items():
        current = existing.get(key)
        if current is None:
            service.events().insert(calendarId=calendar_id, body=body).execute()
            created += 1
        elif _event_differs(current, body):
            service.events().update(calendarId=calendar_id, eventId=current["id"], body=body).execute()
            updated += 1

    for key, ev in existing.items():
        if key not in desired:
            service.events().delete(calendarId=calendar_id, eventId=ev["id"]).execute()
            deleted += 1

    # stdout только когда есть о чём сказать: тишина = ноль стоимости в
    # cron --no-agent режиме и ноль лишних уведомлений Михаилу.
    if created or updated or deleted:
        bits = []
        if created:
            bits.append(f"добавлено {created}")
        if updated:
            bits.append(f"обновлено {updated}")
        if deleted:
            bits.append(f"убрано {deleted}")
        print(f"📚 Расписание обновилось: {', '.join(bits)}.")


if __name__ == "__main__":
    main()
