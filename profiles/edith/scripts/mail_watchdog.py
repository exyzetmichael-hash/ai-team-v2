#!/usr/bin/env python3
"""Сторож почты EDITH — НЕ агент, ни разу не вызывает LLM.

Зачем этот файл отдельно от MORNING.md/EVENING.md: Михаил проебал задачи,
потому что письмо от Политеха (логин/пароль) ждало вечерней проверки почты
по расписанию. Нужно узнавать о важном сразу, а не по расписанию — но
поднимать полноценный агентный ход на каждую проверку (раз в 15 минут =
96 ходов в день) означало бы платить за системный промпт + инструменты
на каждый пустой прогон, даже когда писем нет. Дороже, чем вся EDITH
целиком за месяц.

Правильная схема — встроенный в Hermes паттерн "cron --script --no-agent"
(cron/scheduler.py, ищи "Classic watchdog pattern"): скрипт запускается
БЕЗ модели, его stdout доставляется в Telegram буквально, если непустой.
Пустой stdout = полная тишина, ноль токенов, ноль стоимости.

Использует ТОТ ЖЕ OAuth-токен, что уже настроен для Calendar/Sheets
(google_api.py, gmail-скоупы там уже есть в общем списке разрешений) —
никакой новой авторизации заводить не нужно.

Дедуп через watchdog_seen_mail.json — чтобы не напоминать об одном и том
же письме на каждом 15-минутном прогоне.

ЗАПУСК (вручную, для проверки):
  HERMES_HOME=~/.hermes/profiles/edith python3 mail_watchdog.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

# Разрешаем переопределить для ручного теста/другого профиля; по умолчанию —
# профиль edith, тот же, что резолвит google_api.py при штатном запуске.
EDITH_HOME = Path(os.environ.get("HERMES_HOME", "") or (Path.home() / ".hermes" / "profiles" / "edith"))
GOOGLE_API = EDITH_HOME / "skills" / "productivity" / "google-workspace" / "scripts" / "google_api.py"

# ⚠️ Намеренно НЕ sys.executable. google_api.py использует googleapiclient,
# который стоит только в venv самого Hermes — если этот сторож запустят
# голым системным python3 (вручную для теста, или если cron когда-нибудь
# станет резолвить интерпретатор иначе), sys.executable окажется без
# нужных пакетов и упадёт с ModuleNotFoundError. Идём напрямую в venv,
# который точно есть — это тот же путь, что в ExecStart всех systemd-юнитов
# Hermes на этом сервере. Раз в жизни этот путь мог измениться при
# переустановке — если сторож молчит вместо нормальной работы, первым
# делом проверь, что он существует.
_VENV_PYTHON = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python3"
PYTHON_BIN = str(_VENV_PYTHON) if _VENV_PYTHON.exists() else sys.executable
SEEN_FILE = EDITH_HOME / "watchdog_seen_mail.json"
MAX_SEEN_KEEP = 500  # не даём файлу расти бесконечно

# Ключевые слова — то, что Михаил прямо назвал важным (Политех, вуз,
# документы, деньги, дедлайны/заявки). Держим широко: цена ложного
# срабатывания — одно лишнее уведомление, цена пропуска — прощёлканный
# логин от вуза, как уже было один раз.
KEYWORDS = [
    "политех", "politeh", "spbstu", "приёмн", "приемн", "деканат",
    "личный кабинет абитуриент", "зачисл", "поступлен",
    "вуз", "университет",
    "документ", "справк", "заявлен",
    "счёт", "счет", "оплат", "платёж", "платеж", "инвойс", "invoice",
    "дедлайн", "deadline", "срок подачи",
]

# Gmail-метки, которые заведомо шум — реклама и соцсети почти никогда не
# важны, даже если случайно зацепили ключевое слово.
NOISE_LABELS = {"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "SPAM"}


def _load_seen() -> set[str]:
    try:
        return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
    except Exception:
        return set()


def _save_seen(seen: set[str]) -> None:
    try:
        SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        trimmed = list(seen)[-MAX_SEEN_KEEP:]
        SEEN_FILE.write_text(json.dumps(trimmed), encoding="utf-8")
    except Exception as exc:
        # В stderr, не в stdout — в Telegram это не попадёт, но останется
        # видно в journalctl, если что-то системно сломалось.
        print(f"[сторож почты] не смог сохранить состояние: {exc}", file=sys.stderr)


def _fetch_unread() -> list[dict]:
    if not GOOGLE_API.exists():
        print(f"[сторож почты] не нашёл google_api.py по пути {GOOGLE_API}", file=sys.stderr)
        return []
    env = dict(os.environ)
    env["HERMES_HOME"] = str(EDITH_HOME)
    try:
        result = subprocess.run(
            [PYTHON_BIN, str(GOOGLE_API), "gmail", "search", "is:unread newer_than:2d", "--max", "20"],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
    except Exception as exc:
        print(f"[сторож почты] не смог вызвать google_api.py: {exc}", file=sys.stderr)
        return []
    if result.returncode != 0:
        print(f"[сторож почты] google_api.py упал (код {result.returncode}): {result.stderr[:2000]}", file=sys.stderr)
        return []
    try:
        return json.loads(result.stdout)
    except Exception as exc:
        print(f"[сторож почты] не распарсил вывод google_api.py: {exc}. Вывод: {result.stdout[:2000]}", file=sys.stderr)
        return []


def _is_important(msg: dict) -> bool:
    labels = set(msg.get("labels") or [])
    if labels & NOISE_LABELS:
        return False
    haystack = f"{msg.get('subject', '')} {msg.get('from', '')} {msg.get('snippet', '')}".lower()
    return any(kw in haystack for kw in KEYWORDS)


def main() -> None:
    seen = _load_seen()
    messages = _fetch_unread()

    new_important = [
        m for m in messages
        if m.get("id") and m["id"] not in seen and _is_important(m)
    ]

    # Отмечаем ВСЕ увиденные письма как просмотренные (не только важные) —
    # иначе неважное письмо будет каждый раз заново проверяться зря, а
    # главное, если оно вдруг попадёт в keywords задним числом (человек
    # дополнит список), не должно триггернуть уведомление о письме
    # недельной давности.
    for m in messages:
        if m.get("id"):
            seen.add(m["id"])
    _save_seen(seen)

    if not new_important:
        return  # тишина: пустой stdout → cron ничего не пришлёт, 0 токенов

    lines = ["📧 Похоже, важное письмо:"]
    for m in new_important[:5]:
        sender = (m.get("from") or "?").split("<")[0].strip()
        subject = m.get("subject") or "(без темы)"
        lines.append(f"— «{subject}» — от {sender}")
    if len(new_important) > 5:
        lines.append(f"...и ещё {len(new_important) - 5}")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
