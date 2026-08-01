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

АРХИТЕКТУРА:
  Telegram guest_message
    --(getUpdates, этот процесс, отдельный от гейтвея EDITH)-->
  EDITH через её собственный /v1/chat/completions
    (тот же самый systemd-сервис hermes-gateway-edith, платформа api_server,
    порт по умолчанию 8642, локально — см. config.yaml)
    --(текст ответа)-->
  answerGuestQuery
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

⚠️ ТОЧНЫЙ ФОРМАТ answerGuestQuery ПОДТВЕРЖДЁН НЕ ДО КОНЦА. Update.guest_message,
Message.guest_query_id, Message.guest_bot_caller_user/chat — сверены с
первоисточником (core.telegram.org/bots/api, вставлено пользователем в чат
2026-07-31, раздел Bot API 10.0 changelog + Update/Message). А вот раздел
Methods с точной сигнатурой answerGuestQuery не был под рукой — известно
из вторичных источников только «guest_query_id (str) + result
(types.InlineQueryResult)», без полной спецификации. Ниже собран
разумный InlineQueryResultArticle. Если первый живой вызов упадёт —
проверить точную сигнатуру на core.telegram.org/bots/api (у самого агента
он отдаёт 403 из песочницы, открывать нужно из обычного браузера) и
поправить только _answer_guest_query — остальной файл трогать не нужно.

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


BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN")
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

    text = (guest_msg.get("text") or "").strip()
    guest_query_id = guest_msg.get("guest_query_id", "")
    caller = guest_msg.get("guest_bot_caller_user") or {}
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
