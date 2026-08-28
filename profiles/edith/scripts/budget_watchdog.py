#!/usr/bin/env python3
"""Сторож лимитов по деньгам — НЕ агент, ни разу не вызывает LLM.

Тот же паттерн, что сторож почты и синхронизация расписания: скрипт читает
Google-таблицу, сравнивает с лимитами, печатает в stdout только если есть
что сказать. Пустой stdout = тишина = ноль стоимости на cron --no-agent.

ЗАЧЕМ
-----
Михаил (2026-08-24): деньги «пока вообще нету в моей жизни, и это очень
плохо — уже переехал, начал жить самостоятельно, но учёта трат нет и очень
много лишних покупок». Просто видеть цифры в таблице после факта не
помогает — решение принимается ДО покупки. Сторож ловит момент, когда
категория пробила порог, и предупреждает сразу, а не когда месяц уже сведён.

ДВА ЛИСТА
---------
`Операции` — журнал трат, пишет finance_webhook.py при каждой распознанной
покупке (см. docs/finance-capture.md).

`Лимиты` — Михаил заполняет сам (его явное решение при обсуждении плана —
не считать за него автоматически по истории трат): Категория | Лимит/день |
Лимит/неделя | Лимит/месяц. Пустая ячейка = лимит на этот период не задан,
период не проверяется для этой категории. Лист заводится этим скриптом при
первом запуске, если его ещё нет — пустой, только с заголовками, чтобы было
куда вписывать цифры.

ДВА ПОРОГА, ОДНО ПРЕДУПРЕЖДЕНИЕ НА КАЖДЫЙ ЗА ПЕРИОД
----------------------------------------------------
«Приближается» (⩾90%) — пока ещё можно притормозить до факта перерасхода.
«Превышено» (⩾100%) — по факту. Оба — ОДИН РАЗ за период (день/неделя/месяц)
на категорию: state-файл помнит, за что уже предупреждали, и не долбит
повторно на каждый прогон, пока период не сменится. Без этого сторож в
режиме "категория весь день красная" слал бы сообщение каждый час.

ПОЯС
----
Все границы периодов — по Europe/Moscow, не по поясу сервера (CEST) и не по
профильному поясу EDITH. Деньги Михаила живут по его реальному календарю в
Питере. Тот же принцип, что в politeh_schedule.py и в дате самой операции
(см. MONEY_TZ в finance_webhook.py) — пояс всегда явный, никогда из
окружения.

ЗАПУСК
------
  hermes -p edith cron create "every hour" \
    --script budget_watchdog.py --no-agent \
    --name "Сторож лимитов" --deliver telegram

Раз в час достаточно: решение о покупке принимается не быстрее, а лишний
трафик Sheets API (квота, не деньги) ни к чему. Вручную для проверки:
  HERMES_HOME=~/.hermes/profiles/edith python3 budget_watchdog.py
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

EDITH_HOME = Path(os.environ.get("HERMES_HOME", "") or (Path.home() / ".hermes" / "profiles" / "edith"))
TOKEN_PATH = EDITH_HOME / "google_token.json"
FINANCE_CONFIG_PATH = EDITH_HOME / "finance_webhook.json"
STATE_PATH = EDITH_HOME / "budget_watchdog_state.json"

OPERATIONS_SHEET = "Операции"
LIMITS_SHEET = "Лимиты"
LIMITS_HEADER = ["Категория", "Лимит/день", "Лимит/неделя", "Лимит/месяц"]

# Деньги считаются по календарю Питера — см. докстринг выше и MONEY_TZ в
# finance_webhook.py (та же зона, тот же принцип, отдельная константа
# только потому, что это отдельный файл без общего модуля между ними).
MONEY_TZ = ZoneInfo("Europe/Moscow")

WARN_THRESHOLD = 0.9  # «приближается»
OVER_THRESHOLD = 1.0  # «превышено»

PERIODS = ("день", "неделя", "месяц")


def _load_finance_config() -> dict:
    try:
        return json.loads(FINANCE_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[бюджет] нет {FINANCE_CONFIG_PATH}: {exc}", file=sys.stderr)
        sys.exit(1)


def _sheets_service():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    if not TOKEN_PATH.exists():
        print(f"[бюджет] нет токена Google по пути {TOKEN_PATH}", file=sys.stderr)
        sys.exit(1)
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH))
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
        else:
            print("[бюджет] токен Google невалиден и не обновляется", file=sys.stderr)
            sys.exit(1)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _ensure_limits_sheet(service, spreadsheet_id: str) -> None:
    """Заводит лист «Лимиты» с заголовками, если его ещё нет.

    Только заголовки — цифры Михаил вписывает сам (его явное решение: не
    предлагать лимиты автоматически по истории трат, слишком рано на
    основе недели данных гадать бюджет на месяц).
    """
    meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    titles = {s["properties"]["title"] for s in meta.get("sheets", [])}
    if LIMITS_SHEET in titles:
        return
    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": LIMITS_SHEET}}}]},
    ).execute()
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id, range=f"{LIMITS_SHEET}!A1",
        valueInputOption="USER_ENTERED", body={"values": [LIMITS_HEADER]},
    ).execute()
    print(f"[бюджет] создал лист «{LIMITS_SHEET}» — впиши туда лимиты по категориям", file=sys.stderr)


def _read_limits(service, spreadsheet_id: str) -> dict[str, dict[str, float]]:
    """Категория → {"день": лимит|None, "неделя": ..., "месяц": ...}."""
    resp = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=f"{LIMITS_SHEET}!A2:D",
    ).execute()
    limits: dict[str, dict[str, Optional[float]]] = {}
    for row in resp.get("values", []):
        if not row or not row[0].strip():
            continue
        category = row[0].strip()
        cells = row[1:4] + [""] * (3 - len(row[1:4]))
        parsed: dict[str, Optional[float]] = {}
        for period, cell in zip(PERIODS, cells):
            cell = (cell or "").strip().replace(",", ".").replace(" ", "")
            try:
                parsed[period] = float(cell) if cell else None
            except ValueError:
                print(f"[бюджет] не понял лимит «{cell}» для «{category}»/{period} — пропускаю", file=sys.stderr)
                parsed[period] = None
        limits[category] = parsed
    return limits


def _read_operations(service, spreadsheet_id: str) -> list[dict]:
    resp = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=f"{OPERATIONS_SHEET}!A2:F",
    ).execute()
    ops = []
    for row in resp.get("values", []):
        if len(row) < 4:
            continue
        date_str, op_type = row[0].strip(), row[1].strip()
        category = row[2].strip() if len(row) > 2 else ""
        amount_raw = row[3].strip() if len(row) > 3 else ""
        if op_type != "Расход" or not category or not amount_raw:
            continue
        try:
            date = dt.date.fromisoformat(date_str[:10])
        except ValueError:
            continue
        try:
            amount = float(amount_raw.replace(",", ".").replace(" ", ""))
        except ValueError:
            continue
        ops.append({"date": date, "category": category, "amount": amount})
    return ops


def _period_bounds(today: dt.date) -> dict[str, tuple[dt.date, dt.date]]:
    week_start = today - dt.timedelta(days=today.weekday())  # понедельник
    month_start = today.replace(day=1)
    return {
        "день": (today, today),
        "неделя": (week_start, today),
        "месяц": (month_start, today),
    }


def _spent_by_category_period(ops: list[dict], bounds: dict[str, tuple[dt.date, dt.date]]) -> dict[str, dict[str, float]]:
    spent: dict[str, dict[str, float]] = {}
    for op in ops:
        cat = spent.setdefault(op["category"], {p: 0.0 for p in PERIODS})
        for period, (start, end) in bounds.items():
            if start <= op["date"] <= end:
                cat[period] += op["amount"]
    return spent


# --------------------------------------------------------------------------
# состояние (дедуп предупреждений)
# --------------------------------------------------------------------------

def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        print(f"[бюджет] не смог сохранить состояние: {exc}", file=sys.stderr)


def _period_key(period: str, today: dt.date) -> str:
    """Ключ периода для сброса дедупа при смене дня/недели/месяца.

    ISO-неделя, не просто понедельник: год тоже должен входить в ключ,
    иначе 30 декабря и 30 декабря год спустя случайно совпадут по неделе.
    """
    if period == "день":
        return today.isoformat()
    if period == "неделя":
        y, w, _ = today.isocalendar()
        return f"{y}-W{w:02d}"
    return f"{today.year}-{today.month:02d}"


def main() -> None:
    finance_cfg = _load_finance_config()
    spreadsheet_id = finance_cfg.get("spreadsheet_id")
    if not spreadsheet_id:
        print("[бюджет] в finance_webhook.json нет spreadsheet_id", file=sys.stderr)
        sys.exit(1)

    service = _sheets_service()
    _ensure_limits_sheet(service, spreadsheet_id)
    limits = _read_limits(service, spreadsheet_id)

    if not limits:
        return  # лист пустой — Михаил ещё не вписал лимиты, тихо ждём

    today = dt.datetime.now(MONEY_TZ).date()
    bounds = _period_bounds(today)
    ops = _read_operations(service, spreadsheet_id)
    spent = _spent_by_category_period(ops, bounds)

    state = _load_state()
    warn_state: dict[str, str] = state.get("warned", {})
    # {"категория|период|уровень": period_key последнего предупреждения}

    over_lines: list[str] = []
    warn_lines: list[str] = []

    for category, per_period_limit in limits.items():
        for period in PERIODS:
            limit = per_period_limit.get(period)
            if not limit:
                continue
            spent_amount = spent.get(category, {}).get(period, 0.0)
            ratio = spent_amount / limit
            pkey = _period_key(period, today)

            for threshold, level, bucket in (
                (OVER_THRESHOLD, "превышено", over_lines),
                (WARN_THRESHOLD, "приближается", warn_lines),
            ):
                if ratio < threshold:
                    continue
                state_key = f"{category}|{period}|{level}"
                if warn_state.get(state_key) == pkey:
                    continue  # уже предупреждали в этом периоде — не дублируем
                warn_state[state_key] = pkey
                bucket.append(
                    f"— {category}, {period}: {spent_amount:.0f} из {limit:.0f} ₽ "
                    f"({ratio * 100:.0f}%)"
                )
                break  # более сильный порог сработал — «приближается» на тот же период не нужен

    state["warned"] = warn_state
    _save_state(state)

    if not over_lines and not warn_lines:
        return  # тишина: пустой stdout → cron ничего не пришлёт, 0 токенов

    lines: list[str] = []
    if over_lines:
        lines.append("💸 Превышен лимит:")
        lines.extend(over_lines)
    if warn_lines:
        if lines:
            lines.append("")
        lines.append("⚠️ Приближается к лимиту:")
        lines.extend(warn_lines)
    print("\n".join(lines))


if __name__ == "__main__":
    main()
