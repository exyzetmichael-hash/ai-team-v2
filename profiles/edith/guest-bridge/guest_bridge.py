#!/usr/bin/env python3
"""Мост между Telegram Guest Bots (Bot API 10.0, поле guest_message) и EDITH.

ЗАЧЕМ ЭТОТ ФАЙЛ СУЩЕСТВУЕТ. Сам Hermes ничего не знает о guest_message —
проверено по исходнику (снапшот 2026-07-24, уже после релиза функции
Telegram 8 мая 2026): поле не встречается нигде в plugins/platforms/telegram/
adapter.py и вообще в репозитории. В самом Hermes это отслеживается как
issue #21587 («No urgency — documenting for roadmap awareness»), разработка
не начата. Дожидаться некого — этот файл узкий самостоятельный обход поверх
официального Telegram Bot API 10.0, а не патч Hermes и не форк адаптера.

⚠️ guest_mode в config.yaml EDITH (gateway.platforms.telegram.extra) — ЭТО
ДРУГАЯ ВЕЩЬ, не имеющая отношения к данному файлу. Тот параметр — старый,
свой у Hermes, ослабляет проверку «та ли это группа» только для групп, где
бот УЖЕ состоит участником. Гостевые упоминания из ЛЮБОГО чата, даже без
членства бота, — то, что реализует именно этот мост.

⚠️ ОТДЕЛЬНЫЙ БОТ, НЕ ТОКЕН EDITH. Первая версия этого файла читала тот же
TELEGRAM_BOT_TOKEN, что и сам гейтвей EDITH — это сломано в принципе:
Telegram разрешает только ОДНО активное подключение getUpdates на токен
бота одновременно (проверено вживую — 409 Conflict сразу же при запуске
рядом с работающим гейтвеем). Independent от allowed_updates, это лимит
на уровне соединения, а не типа апдейта. Хуже: конкурирующий поллинг с тем
же токеном подтверждённо может довести retry-логику самого гейтвея Hermes
(adapter.py, MAX_CONFLICT_RETRIES) до fatal-обрыва основного бота. Поэтому
этот мост работает под ОТДЕЛЬНЫМ ботом (свой токен GUEST_BOT_TOKEN,
заведённый через @BotFather с тем же именем/аватаркой, что и у EDITH, но
другим юзернеймом) — у него собственный, ни с кем не разделяемый слот
getUpdates. Упоминать в чате нужно именно этот второй юзернейм.

АРХИТЕКТУРА:
  Telegram guest_message (на боте-двойнике, GUEST_BOT_TOKEN)
    --(getUpdates, этот процесс, полностью отдельный токен от гейтвея EDITH)-->
  EDITH через её собственный /v1/chat/completions
    (systemd-сервис hermes-gateway-edith, платформа api_server,
    порт по умолчанию 8642, локально — см. config.yaml)
    --(текст ответа)-->
  answerGuestQuery (тем же ботом-двойником, GUEST_BOT_TOKEN)
    --> Telegram --> пользователь, который упомянул бота.

⚠️⚠️ БЕЗОПАСНОСТЬ — САМОЕ ВАЖНОЕ В ЭТОМ ФАЙЛЕ. guest_message приходит от
ЛЮБОГО пользователя Telegram, который написал @<юзернейм бота> в любом чате,
— это идёт В ОБХОД allowlist'а самого Hermes (TELEGRAM_ALLOWED_USERS),
потому что здесь используется прямой HTTP к Telegram Bot API, а не адаптер
Hermes (plugins/platforms/telegram/adapter.py), где та проверка встроена.
Проверка отправителя ниже (ALLOWED_USER_IDS, та же переменная
TELEGRAM_ALLOWED_USERS) — единственное, что не даёт EDITH отвечать
незнакомцам чужими деньгами (квота OpenRouter Михаила) и её личным
контекстом. НЕ УДАЛЯТЬ и не делать опциональной.

⚠️ РЕАЛЬНАЯ ФОРМА guest_message (живой прогон 2026-08-01, группа, не личка):
{"message_id", "from": {id, is_bot, first_name, username, language_code},
"chat": {id, title, type, all_members_are_administrators}, "date",
"guest_query_id", "text", "entities"}. НЕТ guest_bot_caller_user/chat, как
предполагалось по одному только changelog'у Bot API 10.0 — отправитель и
чат лежат в обычных "from"/"chat", ровно как в стандартном Message. Урок:
поле guest_query_id совпало с предположением, а вложенные caller-объекты —
нет; предположения по changelog без раздела Methods подтверждаются только
живым прогоном.

✅ answerGuestQuery ПОДТВЕРЖДЁН ЖИВЬЁМ (2026-08-01, группа "а", @yux_28):
InlineQueryResultArticle-форма ниже принята Telegram с ok=true с первой
попытки, ответ дошёл до отправителя. Полный цикл (guest_message → EDITH →
answerGuestQuery → доставка) пройден end-to-end.

ЗАПУСК: отдельный systemd-юнит (hermes-guest-bridge.service, см. рядом в
этой же папке), НЕ часть gateway-сервиса EDITH — если этот скрипт упадёт,
телеграм-бот в личке продолжает работать как ни в чём не бывало.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s guest-bridge %(levelname)s %(message)s",
)
logger = logging.getLogger("guest_bridge")

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


def _env(name: str, *, required: bool = True, default: str = "") -> str:
    value = os.environ.get(name, default).strip()
    if required and not value:
        logger.error("Отсутствует обязательная переменная окружения: %s", name)
        sys.exit(1)
    return value


BOT_TOKEN = _env("GUEST_BOT_TOKEN")
API_SERVER_KEY = _env("API_SERVER_KEY")
API_SERVER_PORT = _env("API_SERVER_PORT", required=False, default="8642")
API_SERVER_URL = f"http://127.0.0.1:{API_SERVER_PORT}/v1/chat/completions"

# Та же переменная, что уже стоит у самого EDITH в .env — проверяется здесь
# ЗАНОВО, потому что этот процесс говорит с Telegram напрямую, в обход
# adapter.py, где эта проверка обычно происходит. См. предупреждение выше.
_allowed_raw = _env("TELEGRAM_ALLOWED_USERS", required=False, default="")
ALLOWED_USER_IDS = {uid.strip() for uid in _allowed_raw.split(",") if uid.strip()}

OFFSET_FILE = Path(
    os.environ.get(
        "GUEST_BRIDGE_OFFSET_FILE",
        str(Path.home() / ".hermes" / "profiles" / "edith" / "guest_bridge_offset"),
    )
)


def _load_offset() -> int:
    try:
        return int(OFFSET_FILE.read_text().strip())
    except Exception:
        return 0


def _save_offset(offset: int) -> None:
    try:
        OFFSET_FILE.parent.mkdir(parents=True, exist_ok=True)
        OFFSET_FILE.write_text(str(offset))
    except Exception as exc:
        logger.warning("Не удалось сохранить offset (%s) — при рестарте возможны повторы", exc)


def _get_updates(offset: int, client: httpx.Client) -> list[dict[str, Any]]:
    url = TELEGRAM_API.format(token=BOT_TOKEN, method="getUpdates")
    # JSON-тело, не query-string — так allowed_updates (массив) сериализуется
    # без ручной возни с query-параметрами.
    payload = {"offset": offset, "timeout": 30, "allowed_updates": ["guest_message"]}
    resp = client.post(url, json=payload, timeout=40.0)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        logger.error("getUpdates вернул ok=false: %s", data)
        return []
    return data.get("result", [])


def _ask_edith(text: str) -> Optional[str]:
    try:
        resp = httpx.post(
            API_SERVER_URL,
            headers={"Authorization": f"Bearer {API_SERVER_KEY}"},
            json={"messages": [{"role": "user", "content": text}]},
            timeout=120.0,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except Exception as exc:
        logger.error("Ошибка обращения к EDITH (/v1/chat/completions): %s", exc)
        return None


def _answer_guest_query(guest_query_id: str, text: str, client: httpx.Client) -> None:
    """⚠️ Формат result собран по неполным источникам — см. docstring файла."""
    url = TELEGRAM_API.format(token=BOT_TOKEN, method="answerGuestQuery")
    payload = {
        "guest_query_id": guest_query_id,
        "result": {
            "type": "article",
            "id": guest_query_id,
            "title": "EDITH",
            "input_message_content": {"message_text": text[:4000]},
        },
    }
    resp = client.post(url, json=payload, timeout=15.0)
    ok = False
    try:
        ok = bool(resp.json().get("ok"))
    except Exception:
        pass
    if resp.status_code != 200 or not ok:
        logger.error(
            "answerGuestQuery вернул ошибку (%s): %s — см. предупреждение в "
            "docstring про неподтверждённый формат result",
            resp.status_code,
            resp.text,
        )
    else:
        logger.info("Ответ отправлен успешно")


def _handle_guest_message(update: dict[str, Any], client: httpx.Client) -> None:
    guest_msg = update.get("guest_message")
    if not guest_msg:
        return

    raw_text = guest_msg.get("text") or ""
    entities = guest_msg.get("entities") or []
    # Упоминание бота ("@юзернейм") — часть текста по entity type=mention,
    # для EDITH это шум, не часть самого запроса. Вырезаем именно entity-диапазон,
    # а не строкой by convention, чтобы не сломаться на mention не в начале текста.
    mention_spans = [
        (e["offset"], e["offset"] + e["length"])
        for e in entities
        if e.get("type") == "mention"
    ]
    if mention_spans:
        chars = list(raw_text)
        for start, end in sorted(mention_spans, reverse=True):
            del chars[start:end]
        raw_text = "".join(chars)
    text = raw_text.strip()

    guest_query_id = guest_msg.get("guest_query_id", "")
    caller = guest_msg.get("from") or {}
    caller_id = str(caller.get("id", ""))
    caller_name = caller.get("username") or caller.get("first_name") or caller_id

    if not guest_query_id:
        logger.warning("guest_message без guest_query_id, пропускаю: %s", update)
        return

    # ── Sender allowlist — см. ⚠️⚠️ БЕЗОПАСНОСТЬ в начале файла. ──────────
    if ALLOWED_USER_IDS and caller_id not in ALLOWED_USER_IDS:
        logger.info("Отклонено: %s (id=%s) не в TELEGRAM_ALLOWED_USERS", caller_name, caller_id)
        return

    if not text:
        logger.info("Пустой текст от %s, пропускаю", caller_name)
        return

    logger.info("Гостевой запрос от %s: %s", caller_name, text[:120])
    answer = _ask_edith(text)
    if answer is None:
        answer = "Не смогла обработать запрос — что-то сломалось на моей стороне, попробуй ещё раз."
    _answer_guest_query(guest_query_id, answer, client)


def main() -> None:
    logger.info(
        "Guest bridge запущен. allowlist отправителей: %s",
        ALLOWED_USER_IDS or "(ПУСТО — отвечает вообще всем, почти наверняка не то, что нужно)",
    )
    offset = _load_offset()
    with httpx.Client() as client:
        while True:
            try:
                updates = _get_updates(offset, client)
            except Exception as exc:
                logger.error("getUpdates упал: %s — жду 5с и пробую снова", exc)
                time.sleep(5)
                continue
            for update in updates:
                offset = max(offset, update.get("update_id", 0) + 1)
                try:
                    _handle_guest_message(update, client)
                except Exception:
                    logger.exception("Ошибка обработки апдейта, продолжаю цикл")
            if updates:
                _save_offset(offset)


if __name__ == "__main__":
    main()
