#!/usr/bin/env python3
"""Приёмник банковских уведомлений с телефона → лист «Операции».

ЗАЧЕМ
-----
Михаил прямо сказал: «щас мне придётся писать EDITH по каждой мелкой трате,
я это дело заброшу очень скоро». Так и будет — ручной ввод трат бросают все.
Значит трата должна попадать в таблицу без единого действия человека.

СХЕМА
-----
    телефон (Android, MacroDroid ловит пуш банка)
      → HTTP POST по Tailscale на этот сервис
      → разбор текста регулярками (БЕЗ LLM, ноль стоимости)
      → строка в лист «Операции» Google-таблицы

Почему MacroDroid, а не своё приложение: NotificationListenerService — это
полноценное Android-приложение, сборка, подпись, сайдлоад и поддержка при
каждом обновлении системы. MacroDroid делает ровно то же самое из коробки,
настраивается за десять минут и не требует ни строчки кода. Если однажды
упрёмся в её ограничения — своё приложение всегда можно написать поверх
этого же протокола, сервис не изменится.

Почему Tailscale, а не публичный порт: сюда прилетают все траты Михаила.
Публичный эндпоинт с деньгами — это то, что надо защищать всерьёз
(домен, TLS, ротация ключей, рейт-лимиты, мониторинг). В tailnet сервис
физически недоступен снаружи, и остаётся один общий секрет как защита от
чужого устройства в самой сети.

⚠️ ПОРЯДОК ВНЕДРЕНИЯ — СНАЧАЛА СЫРЬЁ, ПОТОМ РАЗБОР
--------------------------------------------------
Форматы пушей у банков разные и меняются. Писать регулярки по памяти —
гарантированный способ намолотить мусора в таблицу, а чинить таблицу
задним числом дороже, чем подождать.

Поэтому сервис ВСЕГДА сначала пишет сырое уведомление в raw-лог, и только
потом пробует разобрать. Первые день-два держи RAW_ONLY=1 (см. конфиг):
в таблицу не пойдёт ничего, зато накопятся настоящие примеры, по которым
регулярки допишутся уже точно, а не на глаз.

Неразобранное не теряется никогда: оно копится в отдельном файле, и EDITH
может показать его пачкой и спросить, что это было.

ЗАПУСК
------
systemd-юнит: ops/hermes-finance-webhook.service.template
Вручную для теста:
  HERMES_HOME=~/.hermes/profiles/edith python3 finance_webhook.py --port 8765
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

# Деньги считаются по календарю Михаила (Питер), не по поясу сервера.
# ⚠️ Не sys.date.today() — сервер живёт в CEST, разница с Москвой всего час,
# но у бюджетного сторожа (budget_watchdog.py) день/неделя/месяц считаются
# по Europe/Moscow, и трата, записанная с датой сервера, у границы суток
# попадала бы не в тот день. Тот же принцип, что и явный пояс в
# politeh_schedule.py — TZ никогда не берётся из окружения.
MONEY_TZ = ZoneInfo("Europe/Moscow")

EDITH_HOME = Path(os.environ.get("HERMES_HOME", "") or (Path.home() / ".hermes" / "profiles" / "edith"))
TOKEN_PATH = EDITH_HOME / "google_token.json"
CONFIG_PATH = EDITH_HOME / "finance_webhook.json"
RULES_PATH = EDITH_HOME / "finance_rules.json"

RAW_LOG = EDITH_HOME / "finance_raw.jsonl"
UNPARSED_LOG = EDITH_HOME / "finance_unparsed.jsonl"
SEEN_PATH = EDITH_HOME / "finance_seen.json"

SHEET_NAME = "Операции"
MAX_SEEN_KEEP = 2000

_write_lock = threading.Lock()


# --------------------------------------------------------------------------
# конфиг
# --------------------------------------------------------------------------

def load_config() -> dict:
    """Конфиг сервиса. secret и spreadsheet_id обязательны."""
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[финансы] нет конфига {CONFIG_PATH}: {exc}", file=sys.stderr)
        sys.exit(1)
    for required in ("secret", "spreadsheet_id"):
        if not cfg.get(required):
            print(f"[финансы] в конфиге нет обязательного поля {required!r}", file=sys.stderr)
            sys.exit(1)
    return cfg


def load_rules() -> dict:
    """Правила «продавец → категория». Дополняются по ходу жизни."""
    try:
        return json.loads(RULES_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


# --------------------------------------------------------------------------
# разбор уведомления
# --------------------------------------------------------------------------

# Сумма: «450 ₽», «450.50 руб», «1 234,56 RUR», «450р».
_AMOUNT_RE = re.compile(
    r"(?<![\d.,])(\d{1,3}(?:[  ]\d{3})*(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)\s*"
    r"(?:₽|руб|р\.|р\b|RUB|RUR)",
    re.IGNORECASE,
)

# Слова, по которым определяем направление операции. Порядок важен:
# «Возврат покупки» — это приход, хотя внутри есть слово «покупк».
_INCOME_MARKERS = ["зачислен", "пополнен", "поступил", "возврат", "перевод от", "кэшбэк", "кешбэк", "аванс", "зарплат"]
_EXPENSE_MARKERS = ["покупк", "оплат", "списан", "снятие", "перевод на", "платёж", "платеж"]

# ⚠️ Живые данные 2026-08-29 (Сбербанк): сумма и направление лежат в title,
# не в text, и направление — не слово, а эмодзи в самом начале строки:
# «➕ 1 000 ₽ по СБП от МИХАИЛ ОЛЕГОВИЧ Ю.» / «➖ 648 ₽ FRIKADELNIA_EURO».
# text у Сбера — это остаток и цифры карты («📈 1 015,68 ₽ •• 0946»), там
# денег на распознавание нет вообще. Без этого блока НИ ОДНА сберовская
# операция не проходила бы — уходили бы в unparsed молча.
_EMOJI_DIRECTION_RE = re.compile(r"^\s*(➕|➖)\s*")
_SBP_SENDER_RE = re.compile(r"^по\s+СБП(?:\s+от)?\s*", re.IGNORECASE)

# Хвост уведомления, который продавцом быть не может: всё начиная с
# «Баланс: ...». Режем именно с этих слов, и только с них — раньше сюда
# входила ещё и «Карта», но она стоит в НАЧАЛЕ уведомления, и хвост
# отрезался вместе с настоящим названием магазина.
_TAIL_NOISE = re.compile(r"(баланс|доступно|остаток)\b.*$", re.IGNORECASE)

# Ссылка на карту/счёт — вырезаем точечно, где бы ни стояла.
_CARD_RE = re.compile(r"(карт[аыуе]?|счёт|счет)\s*№?\s*\*?\s*\d{0,4}", re.IGNORECASE)

# Слова-маркеры операции в начале фрагмента («Оплата SPOTIFY» → «SPOTIFY»).
# Повторяющаяся группа намеренно: маркеров подряд бывает несколько —
# «Возврат покупки Пятёрочка» надо очистить от обоих слов, не только первого.
_LEADING_MARKER_RE = re.compile(
    r"^\s*(?:(?:" + "|".join(_INCOME_MARKERS + _EXPENSE_MARKERS) + r")\w*\s*)+",
    re.IGNORECASE,
)


def _norm_amount(raw: str) -> Optional[float]:
    cleaned = raw.replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        return round(float(cleaned), 2)
    except ValueError:
        return None


def _detect_direction(text: str) -> Optional[str]:
    low = text.lower()
    for marker in _INCOME_MARKERS:
        if marker in low:
            return "Доход"
    for marker in _EXPENSE_MARKERS:
        if marker in low:
            return "Расход"
    return None


def _guess_merchant(text: str) -> str:
    """Достаёт название продавца — самую содержательную часть уведомления.

    Намеренно грубо: банки пишут по-разному, а точность здесь не критична.
    Продавец идёт в комментарий операции и в правила категоризации; если
    угадали криво, Михаил поправит строку в таблице, и это не потеря денег,
    а косметика. Сумма и направление — вот что должно быть точным.
    """
    body = _TAIL_NOISE.sub("", text).strip()      # «Баланс: ...» и дальше — не продавец
    body = _CARD_RE.sub(" ", body)                # «Карта *1234» — не продавец
    body = _AMOUNT_RE.sub(" ", body)              # сумма — уже разобрана отдельно

    candidates = []
    for part in re.split(r"[.,;|]| - ", body):
        part = _LEADING_MARKER_RE.sub("", part).strip(" .,;*№")
        # Однобуквенные огрызки и голые числа продавцом не бывают.
        if len(part) < 2 or part.isdigit():
            continue
        candidates.append(part)

    if not candidates:
        return body.strip(" .,;")[:80]
    return max(candidates, key=len)[:80]


def categorize(merchant: str, rules: dict) -> str:
    """Категория по правилам. Неизвестное — «Прочее», без выдумок.

    Сознательно НЕ зовём LLM: разбор трат должен стоить ноль, иначе он
    съест то, ради чего вся эта экономия затевалась. Неизвестных продавцов
    EDITH разберёт пачкой раз в неделю и допишет сюда правило — один вызов
    на десяток трат вместо вызова на каждую.
    """
    low = merchant.lower()
    for needle, category in rules.items():
        if needle.lower() in low:
            return category
    return "Прочее"


def _parse_emoji_title(title: str, rules: dict) -> Optional[dict]:
    """Формат Сбербанка: «➕/➖ <сумма> ₽ <получатель/продавец>» в title.

    Проверяется первым, до общей логики по тексту — у Сбера в text денег
    нет вообще (только остаток), а в title направление задано эмодзи, не
    словом, так что общие маркеры _INCOME_MARKERS/_EXPENSE_MARKERS его не
    поймают.
    """
    m = _EMOJI_DIRECTION_RE.match(title or "")
    if not m:
        return None
    direction = "Доход" if m.group(1) == "➕" else "Расход"

    rest = title[m.end():].strip()
    amount_match = _AMOUNT_RE.search(rest)
    if not amount_match:
        return None
    amount = _norm_amount(amount_match.group(1))
    if amount is None or amount <= 0:
        return None

    merchant = _AMOUNT_RE.sub("", rest).strip(" .,;")
    merchant = _SBP_SENDER_RE.sub("", merchant).strip()  # «по СБП от ИМЯ» -> «ИМЯ»
    if not merchant:
        merchant = "Сбербанк"

    return {
        "type": direction,
        "amount": amount,
        "merchant": merchant,
        "category": categorize(merchant, rules) if direction == "Расход" else "",
        "raw": title,
    }


def parse_notification(text: str, rules: dict, title: str = "") -> Optional[dict]:
    """Текст пуша → операция, или None если не разобралось.

    None здесь — нормальный исход, а не ошибка: уведомление могло быть
    вообще не про деньги (акция банка, сообщение поддержки). Такое уходит
    в UNPARSED_LOG на разбор человеком, но в таблицу не попадает никогда —
    мусор в журнале операций хуже, чем пропущенная трата.
    """
    emoji_result = _parse_emoji_title(title, rules)
    if emoji_result:
        return emoji_result

    if not text:
        return None

    amount_match = _AMOUNT_RE.search(text)
    if not amount_match:
        return None
    amount = _norm_amount(amount_match.group(1))
    if amount is None or amount <= 0:
        return None

    direction = _detect_direction(text)
    if direction is None:
        return None

    merchant = _guess_merchant(text)
    return {
        "type": direction,
        "amount": amount,
        "merchant": merchant,
        "category": categorize(merchant, rules) if direction == "Расход" else "",
        "raw": text,
    }


# --------------------------------------------------------------------------
# дедуп
# --------------------------------------------------------------------------

def _load_seen() -> list[str]:
    try:
        return json.loads(SEEN_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_seen(seen: list[str]) -> None:
    try:
        SEEN_PATH.write_text(json.dumps(seen[-MAX_SEEN_KEEP:]), encoding="utf-8")
    except Exception as exc:
        print(f"[финансы] не смог сохранить дедуп: {exc}", file=sys.stderr)


def notification_id(payload: dict) -> str:
    """Отпечаток уведомления для дедупа.

    Android показывает одно и то же уведомление повторно (обновление,
    перерисовка шторки, перезапуск MacroDroid), и без дедупа одна покупка
    легко превратится в три строки в таблице. Время округляем до минуты:
    один и тот же пуш в пределах минуты — точно один и тот же.
    """
    stamp = (payload.get("posted_at") or "")[:16]
    base = f"{payload.get('package', '')}|{payload.get('text', '')}|{stamp}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:32]


# --------------------------------------------------------------------------
# Google Sheets
# --------------------------------------------------------------------------

def append_operation(spreadsheet_id: str, op: dict) -> None:
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH))
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
        else:
            raise RuntimeError("токен Google невалиден и не обновляется")

    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    # Колонки листа «Операции»: Дата | Тип | Категория | Сумма | Комментарий |
    # Источник дохода (см. docs/finance-sheet-spec.md).
    row = [
        dt.datetime.now(MONEY_TZ).date().isoformat(),
        op["type"],
        op["category"],
        op["amount"],
        f"{op['merchant']} (авто)",
        "Разовое" if op["type"] == "Доход" else "",
    ]
    service.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=f"{SHEET_NAME}!A:F",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def _append_jsonl(path: Path, obj: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    except Exception as exc:
        print(f"[финансы] не смогписать в {path.name}: {exc}", file=sys.stderr)


class Handler(BaseHTTPRequestHandler):
    config: dict = {}
    rules: dict = {}

    def _reply(self, code: int, body: dict) -> None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        # Health-check, чтобы с телефона можно было проверить связь, не
        # присылая фальшивую трату.
        if self.path == "/health":
            self._reply(200, {"ok": True})
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/notification":
            self._reply(404, {"error": "not found"})
            return

        token = self.headers.get("X-Auth-Token", "")
        if token != self.config.get("secret"):
            # Не уточняем, что именно не так — в tailnet может быть чужое
            # устройство, ему не надо подсказывать.
            self._reply(403, {"error": "forbidden"})
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            self._reply(400, {"error": "bad json"})
            return

        text = (payload.get("text") or "").strip()
        title = (payload.get("title") or "").strip()
        # MONEY_TZ, не серверный локальный — иначе received_at путает при
        # диагностике (сервер в CEST, на час позади Москвы), хоть в саму
        # таблицу это поле и не попадает (там дата уже по MONEY_TZ отдельно).
        payload.setdefault("received_at", dt.datetime.now(MONEY_TZ).isoformat(timespec="seconds"))

        with _write_lock:
            # Сырое пишем ВСЕГДА и первым делом — до любой попытки разбора.
            # Если разбор упадёт или окажется кривым, данные всё равно есть
            # и по ним можно восстановить историю и починить регулярки.
            _append_jsonl(RAW_LOG, payload)

            nid = notification_id(payload)
            seen = _load_seen()
            if nid in seen:
                self._reply(200, {"ok": True, "status": "duplicate"})
                return

            if self.config.get("raw_only"):
                seen.append(nid)
                _save_seen(seen)
                self._reply(200, {"ok": True, "status": "raw_only"})
                return

            op = parse_notification(text, self.rules, title=title)
            if op is None:
                _append_jsonl(UNPARSED_LOG, payload)
                seen.append(nid)
                _save_seen(seen)
                self._reply(200, {"ok": True, "status": "unparsed"})
                return

            try:
                append_operation(self.config["spreadsheet_id"], op)
            except Exception as exc:
                # Дедуп НЕ отмечаем: если запись не удалась, при повторной
                # доставке уведомление должно попробовать записаться снова.
                print(f"[финансы] не смог записать в таблицу: {exc}", file=sys.stderr)
                _append_jsonl(UNPARSED_LOG, {**payload, "error": str(exc)})
                self._reply(500, {"error": "sheet write failed"})
                return

            seen.append(nid)
            _save_seen(seen)
            self._reply(200, {"ok": True, "status": "recorded",
                              "type": op["type"], "amount": op["amount"],
                              "category": op["category"]})

    def log_message(self, fmt: str, *args: Any) -> None:
        # Дефолтный лог пишет в stderr полный путь и тело запроса — здесь
        # это означало бы суммы трат в journalctl. Глушим.
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="Приёмник банковских уведомлений")
    parser.add_argument("--host", default=None, help="адрес прослушивания (по умолчанию из конфига)")
    parser.add_argument("--port", type=int, default=None, help="порт (по умолчанию из конфига или 8765)")
    parser.add_argument("--test-parse", metavar="ТЕКСТ", help="разобрать text уведомления и выйти")
    parser.add_argument("--test-title", metavar="ЗАГОЛОВОК", default="",
                        help="title уведомления для --test-parse (у Сбербанка сумма именно тут)")
    args = parser.parse_args()

    rules = load_rules()

    if args.test_parse:
        result = parse_notification(args.test_parse, rules, title=args.test_title)
        print(json.dumps(result, ensure_ascii=False, indent=2) if result else "не разобралось")
        return

    config = load_config()
    host = args.host or config.get("host") or "127.0.0.1"
    port = args.port or config.get("port") or 8765

    Handler.config = config
    Handler.rules = rules

    mode = "ТОЛЬКО СЫРЬЁ (в таблицу не пишем)" if config.get("raw_only") else "боевой (пишем в таблицу)"
    print(f"[финансы] слушаю {host}:{port}, режим: {mode}", file=sys.stderr)
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
