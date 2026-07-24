"""office-gate — дешёвый гейт "моя тема / не моя" для группового офис-чата.

⚠️ ЧЕРНОВИК, НЕ ПРОВЕРЕН ВЖИВУЮ. Написан по документированному контракту
pre_gateway_dispatch (сигнатура, MessageEvent.chat_type,
MessageEvent.reply_to_is_own_message — всё сверено по исходникам Hermes
v0.19.0), но без второго живого агента и реального группового чата
откалибровать его я не могу. См. "План калибровки" в
docs/phase-5-runbook.md — это первое, что нужно проверить руками.

Что делает:
  pre_gateway_dispatch срабатывает на КАЖДОЕ входящее сообщение до того, как
  запускается полноценный (дорогой) агент. Для сообщений в группе/форуме
  (не личка) хук:

    1. Если это ответ на предыдущее сообщение ЭТОГО ЖЕ агента
       (event.reply_to_is_own_message) — пропускает без вопросов.
    2. Если имя профиля явно упомянуто в тексте (грубая, но дешёвая проверка
       без обращения к LLM) — пропускает без вопросов.
    3. Иначе — ОДИН маленький LLM-запрос: "относится ли это сообщение к моей
       роли? да/нет", используя первый абзац SOUL.md как описание роли.
       "нет" -> {"action": "skip"} — агент вообще не запускается, экономия.
       "да" -> обычный запуск.

  ЛИЧНЫЕ СООБЩЕНИЯ (DM) хук не трогает вообще — там пользователь и так пишет
  конкретно этому агенту, гейт был бы только помехой.

  Failure mode специально выбран в сторону "пропустить, а не молчать": если
  что-то пошло не так (нет SOUL.md, классификатор недоступен, любая ошибка) —
  хук возвращает None (обычный запуск), а не skip. Так безопаснее: лишний
  прогон агента стоит немного денег, а ложное молчание — это именно та
  болезнь старого ai-team ("агенты не отвечают, все молчат"), которую мы
  чиним, а не хотим воспроизвести заново.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import httpx
except Exception:  # pragma: no cover
    httpx = None

_CLASSIFY_URL = "https://openrouter.ai/api/v1/chat/completions"

_role_cache = {"text": None, "loaded": False}
_lock = threading.Lock()


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))


def _profile_name() -> str:
    # HERMES_HOME для профиля обычно .../.hermes/profiles/<name> — берём
    # последний компонент пути как имя. Если это дефолтный профиль
    # (просто ~/.hermes), возвращаем "default".
    home = _hermes_home()
    return home.name if home.parent.name == "profiles" else "default"


def _load_role_summary() -> Optional[str]:
    """Первый абзац SOUL.md — краткое описание роли профиля. Кешируется на
    время жизни процесса (SOUL.md не меняется на лету)."""
    with _lock:
        if _role_cache["loaded"]:
            return _role_cache["text"]
    soul_path = _hermes_home() / "SOUL.md"
    text = None
    try:
        if soul_path.exists():
            raw = soul_path.read_text(encoding="utf-8")
            # Первый абзац (до первой пустой строки после заголовка) —
            # достаточно контекста для классификации, не весь файл.
            paragraphs = [p.strip() for p in raw.split("\n\n") if p.strip()]
            text = paragraphs[1] if len(paragraphs) > 1 else (paragraphs[0] if paragraphs else None)
    except Exception as exc:
        logger.debug("office-gate: could not read SOUL.md: %s", exc)
    with _lock:
        _role_cache["text"] = text
        _role_cache["loaded"] = True
    return text


def _classify(role_summary: str, message_text: str) -> Optional[bool]:
    """Возвращает True (моя тема) / False (не моя) / None (не смогли
    классифицировать — вызывающий код должен трактовать это как True,
    т.е. пропустить, см. docstring модуля)."""
    if httpx is None:
        return None
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        return None
    model = os.environ.get("HERMES_OFFICE_GATE_MODEL", "deepseek/deepseek-v4-flash")
    prompt = (
        f"Роль агента: {role_summary}\n\n"
        f"Сообщение в общем чате: {message_text[:500]}\n\n"
        "Относится ли это сообщение к зоне ответственности агента выше? "
        "Ответь ровно одним словом: да или нет."
    )
    try:
        resp = httpx.post(
            _CLASSIFY_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 5,
                "temperature": 0,
            },
            timeout=8.0,
        )
        resp.raise_for_status()
        data = resp.json()
        answer = data["choices"][0]["message"]["content"].strip().lower()
        return answer.startswith("да")
    except Exception as exc:
        logger.debug("office-gate: classification call failed, defaulting to allow: %s", exc)
        return None


def office_gate(event, gateway, session_store, **kwargs):
    try:
        source = getattr(event, "source", None)
        if source is None or getattr(source, "chat_type", "dm") == "dm":
            return None  # личка — не трогаем

        if getattr(event, "reply_to_is_own_message", False):
            return None  # явно отвечают этому агенту

        text = getattr(event, "text", "") or ""
        profile = _profile_name()
        if profile and profile.lower() in text.lower():
            return None  # явно упомянули по имени профиля — без LLM-звонка

        role_summary = _load_role_summary()
        if not role_summary:
            return None  # не смогли прочитать роль — не рискуем молчать

        decision = _classify(role_summary, text)
        if decision is False:
            return {"action": "skip", "reason": "office-gate: not relevant to this profile"}
        return None  # True или None (ошибка) -> обычный запуск
    except Exception as exc:  # хук не должен ронять доставку сообщения
        logger.warning("office-gate: unexpected error, defaulting to allow: %s", exc)
        return None


def register(ctx) -> None:
    ctx.register_hook("pre_gateway_dispatch", office_gate)
