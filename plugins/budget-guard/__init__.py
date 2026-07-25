"""budget-guard — мягкий бюджет-хук для Hermes.

Ставится через ~/.hermes/plugins/budget-guard/ (на профиль, куда его положишь —
скопируй в HERMES_HOME профиля или в общий ~/.hermes/plugins/, если он должен
работать для всех профилей сразу; см. docs/phase-2-runbook.md).

Что делает:
  - раз в HERMES_BUDGET_CHECK_INTERVAL секунд (по умолчанию 900 = 15 мин)
    дёргает GET https://openrouter.ai/api/v1/credits с текущим OPENROUTER_API_KEY;
  - если остаток (total_credits - total_usage) ниже HERMES_BUDGET_WARN_USD
    (по умолчанию $3) — вписывает короткое предупреждение в контекст текущего
    хода через pre_llm_call (агент сам решает, упоминать ли это Михаилу);
  - НИКОГДА не блокирует и не отказывает — только текстовое предупреждение.
    Это принципиально: требование 3.8 брифа — мягкий лимит, "предупредить и
    спросить, не рубить наглухо".
  - при любой ошибке (нет ключа, сеть недоступна, API поменялся) — молча
    ничего не делает, ход идёт как обычно. Бюджет-хук не должен ронять агента.

Ограничение честно: сама проверка стоит один HTTP-запрос раз в 15 минут на
профиль, где стоит плагин — не на каждый ход. Это компромисс между
актуальностью цифры и лишними запросами.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import httpx
except Exception:  # pragma: no cover - httpx всегда есть в Hermes, но не рискуем
    httpx = None

_CREDITS_URL = "https://openrouter.ai/api/v1/credits"

_lock = threading.Lock()
_cache: dict = {"checked_at": 0.0, "remaining": None, "warned_this_period": False}


def _check_interval() -> float:
    try:
        return float(os.environ.get("HERMES_BUDGET_CHECK_INTERVAL", "900"))
    except ValueError:
        return 900.0


def _warn_threshold_usd() -> float:
    try:
        return float(os.environ.get("HERMES_BUDGET_WARN_USD", "3.0"))
    except ValueError:
        return 3.0


def _fetch_remaining_usd() -> Optional[float]:
    """Возвращает остаток в USD или None при любой проблеме. Никогда не бросает."""
    if httpx is None:
        return None
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        return None
    try:
        resp = httpx.get(
            _CREDITS_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=5.0,
        )
        resp.raise_for_status()
        data = resp.json()
        # Документированная форма: {"data": {"total_credits": X, "total_usage": Y}}
        # либо плоская {"total_credits": X, "total_usage": Y} — принимаем обе,
        # т.к. этот эндпоинт не был проверен вживую на нашем ключе (см. runbook).
        payload = data.get("data", data) if isinstance(data, dict) else {}
        total_credits = payload.get("total_credits")
        total_usage = payload.get("total_usage")
        if total_credits is None or total_usage is None:
            logger.debug("budget-guard: unexpected /credits response shape: %r", data)
            return None
        return float(total_credits) - float(total_usage)
    except Exception as exc:
        logger.debug("budget-guard: credits check failed (%s) — skipping this cycle", exc)
        return None


def _get_remaining_cached() -> Optional[float]:
    now = time.time()
    with _lock:
        if now - _cache["checked_at"] < _check_interval() and _cache["checked_at"] > 0:
            return _cache["remaining"]
    remaining = _fetch_remaining_usd()
    with _lock:
        _cache["checked_at"] = now
        if remaining is not None:
            _cache["remaining"] = remaining
        return _cache["remaining"]


def check_budget(
    session_id: str,
    user_message: str,
    conversation_history: list,
    is_first_turn: bool,
    model: str,
    platform: str,
    **kwargs,
):
    try:
        remaining = _get_remaining_cached()
        if remaining is None:
            return None
        threshold = _warn_threshold_usd()
        if remaining >= threshold:
            with _lock:
                _cache["warned_this_period"] = False
            return None
        # Предупреждаем не на каждый ход подряд, а раз за низкий период —
        # иначе это будет мозолить глаза в каждом ответе, пока баланс низкий.
        with _lock:
            if _cache["warned_this_period"]:
                return None
            _cache["warned_this_period"] = True
        return {
            "context": (
                f"[СИСТЕМА, не для показа как есть] Остаток бюджета на LLM-токены "
                f"(OpenRouter): ${remaining:.2f}, порог предупреждения ${threshold:.2f}. "
                f"Это не команда отказывать или молчать — просто имей в виду и, если "
                f"уместно, можешь упомянуть Михаилу мимоходом."
            )
        }
    except Exception as exc:  # защита: хук никогда не должен ронять ход
        logger.debug("budget-guard: unexpected error, skipping: %s", exc)
        return None


def register(ctx) -> None:
    ctx.register_hook("pre_llm_call", check_budget)
