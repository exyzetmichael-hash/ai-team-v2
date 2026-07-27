"""office-gate — гейт «моя тема / не моя» для общего офис-чата.

Задача: в групповом чате, где сидят все агенты команды, на каждое сообщение
должен отвечать тот, кому тема ближе — а не все семеро разом и не никто.

Как это работает. Хук `pre_gateway_dispatch` срабатывает у КАЖДОГО агента на
каждое входящее сообщение, до того как запустится полноценный (дорогой) агент.
Каждый агент решает за себя, без куратора — общего диспетчера тут нет по
дизайну (см. бриф: «без куратора»). Порядок проверок выстроен так, чтобы
дешёвые и однозначные случаи отсекались БЕЗ обращения к модели:

  1. Личка (не группа) — пропускаем всегда. Там и так пишут конкретно этому
     агенту, гейт был бы только помехой.
  2. Это ответ на предыдущее сообщение ЭТОГО агента — пропускаем, разговор
     продолжается с ним.
  3. В тексте явно упомянут ЭТОТ агент («секретарь, ...») — пропускаем без
     вызова модели.
  4. В тексте явно упомянут ДРУГОЙ агент, а этот — нет — молчим без вызова
     модели. Самый частый и самый дешёвый способ убрать перекрёстный шум.
  5. Иначе — один маленький запрос к дешёвой модели: «относится ли это к моей
     зоне ответственности?». «Нет» → агент вообще не запускается.

Про имена (пункты 3-4). Профили названы по-английски (`secretary`, `brain`,
`legal`), а Михаил пишет по-русски и со склонениями («секретарю», «юриста»).
Поэтому сравниваем не с именем профиля, а со списком русских основ слов —
`ROLE_ALIASES` ниже. Сверка идёт по началу слова, так что склонения ловятся
сами: основа «секретар» покрывает и «секретарь», и «секретарю», и «секретарём».

Описание роли для классификатора берётся из `<профиль>/profile.yaml`, ключ
`description` — того же файла, по которому Kanban раздаёт задачи. Это
сознательно: одна формулировка «чем занимается агент» на оба механизма, чтобы
они не разъезжались. Если описания нет — откатываемся на первый абзац SOUL.md.

Failure mode выбран в сторону «пропустить, а не промолчать»: при любой ошибке
(нет описания, классификатор недоступен, что угодно) хук возвращает None, то
есть агент отвечает как обычно. Так безопаснее: лишний прогон стоит немного
денег, а ложное молчание — это ровно та болезнь старого ai-team («агенты не
отвечают»), которую мы чиним, а не воспроизводим заново.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import httpx
except Exception:  # pragma: no cover
    httpx = None

_CLASSIFY_URL = "https://openrouter.ai/api/v1/chat/completions"

# Русские основы слов, по которым Михаил зовёт каждую роль. Сверка идёт по
# началу слова, поэтому склонения ловятся автоматически ("секретар" ->
# секретарь/секретаря/секретарю/секретарём). Английское имя профиля
# добавляется к его списку автоматически, отдельно писать не нужно.
#
# Переопределить можно через переменную окружения HERMES_OFFICE_GATE_ALIASES
# с JSON вида {"secretary": ["секретар", "сек"], ...} — тогда встроенная карта
# игнорируется целиком.
ROLE_ALIASES: dict[str, list[str]] = {
    "secretary": ["секретар"],
    "brain": ["мозг", "брейн"],
    "finance": ["финансист", "финанс"],
    "tutor": ["тьютор", "репетитор", "преподават"],
    "tracker": ["трекер", "трэкер"],
    "research": ["ресёрчер", "ресерчер", "ресёрч", "ресерч", "исследоват"],
    "legal": ["юрист", "юрид", "правовед"],
}

_cache: dict = {"role": None, "role_loaded": False, "aliases": None}
_lock = threading.Lock()


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))


def _profile_name() -> str:
    """Имя текущего профиля из пути HERMES_HOME (.../profiles/<name>)."""
    home = _hermes_home()
    return home.name if home.parent.name == "profiles" else "default"


def _aliases() -> dict[str, list[str]]:
    """Карта {профиль: [основы слов]}, с добавленным именем самого профиля."""
    with _lock:
        if _cache["aliases"] is not None:
            return _cache["aliases"]
    raw = os.environ.get("HERMES_OFFICE_GATE_ALIASES", "").strip()
    table = dict(ROLE_ALIASES)
    if raw:
        try:
            override = json.loads(raw)
            if isinstance(override, dict):
                table = {str(k): [str(x).lower() for x in v] for k, v in override.items()}
        except Exception as exc:
            logger.warning("office-gate: bad HERMES_OFFICE_GATE_ALIASES (%s), using defaults", exc)
    # имя профиля — тоже валидное обращение ("brain, посмотри ...")
    for name in list(table):
        if name.lower() not in table[name]:
            table[name] = table[name] + [name.lower()]
    with _lock:
        _cache["aliases"] = table
    return table


def _mentions(text_lower: str, stems: list[str]) -> bool:
    """True, если к агенту обратились по имени/роли.

    Правило: основа стоит в НАЧАЛЕ слова и после неё не больше трёх букв.
    Ограничение на хвост важно — русские падежные окончания короткие
    («секретар|ю», «мозг|ом», «юрист|а», «секретар|ями» — максимум три буквы),
    а прилагательные и производные длиннее. Без этого ограничения основа
    «мозг» ловилась в «мозговой штурм», и агент считал, что звали его
    (поймано тестом на реальной фразе, а не в теории).
    """
    for stem in stems:
        if not stem:
            continue
        if re.search(r"(?<!\w)" + re.escape(stem) + r"\w{0,3}(?!\w)", text_lower):
            return True
    return False


def _load_role_summary() -> Optional[str]:
    """Описание роли: profile.yaml -> description, иначе первый абзац SOUL.md."""
    with _lock:
        if _cache["role_loaded"]:
            return _cache["role"]

    home = _hermes_home()
    text: Optional[str] = None

    # 1. profile.yaml — тот же источник, по которому Kanban раздаёт задачи
    try:
        meta_path = home / "profile.yaml"
        if meta_path.is_file():
            import yaml
            data = yaml.safe_load(meta_path.read_text(encoding="utf-8")) or {}
            if isinstance(data, dict):
                desc = str(data.get("description") or "").strip()
                if desc:
                    text = desc
    except Exception as exc:
        logger.debug("office-gate: could not read profile.yaml: %s", exc)

    # 2. Фолбэк — первый содержательный абзац SOUL.md (заголовок пропускаем)
    if not text:
        try:
            soul = home / "SOUL.md"
            if soul.is_file():
                raw = soul.read_text(encoding="utf-8")
                paragraphs = [p.strip() for p in raw.split("\n\n") if p.strip()]
                body = [p for p in paragraphs if not p.lstrip().startswith("#")]
                if body:
                    text = body[0]
        except Exception as exc:
            logger.debug("office-gate: could not read SOUL.md: %s", exc)

    with _lock:
        _cache["role"] = text
        _cache["role_loaded"] = True
    return text


def _classify(role_summary: str, message_text: str) -> Optional[bool]:
    """True (моя тема) / False (не моя) / None (не смогли — трактовать как True)."""
    if httpx is None:
        return None
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        return None
    model = os.environ.get("HERMES_OFFICE_GATE_MODEL", "deepseek/deepseek-v4-flash")
    prompt = (
        f"Зона ответственности агента: {role_summary}\n\n"
        f"Сообщение в общем рабочем чате: {message_text[:500]}\n\n"
        "Это сообщение относится к зоне ответственности агента выше?\n"
        "Отвечай «да» ТОЛЬКО если тема явно его. В чате сидят и другие "
        "специалисты — если сообщение скорее к кому-то другому, или это "
        "просто болтовня ни о чём, отвечай «нет».\n"
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
        answer = resp.json()["choices"][0]["message"]["content"].strip().lower()
        return answer.startswith("да")
    except Exception as exc:
        logger.debug("office-gate: classification failed, defaulting to allow: %s", exc)
        return None


def office_gate(event, gateway, session_store, **kwargs):
    try:
        source = getattr(event, "source", None)
        # 1. Личка — не трогаем
        if source is None or getattr(source, "chat_type", "dm") == "dm":
            return None

        # 2. Отвечают на моё же сообщение — разговор продолжается со мной
        if getattr(event, "reply_to_is_own_message", False):
            return None

        text = getattr(event, "text", "") or ""
        text_lower = text.lower()
        me = _profile_name()
        table = _aliases()

        # 3. Позвали меня по имени — отвечаю, без вызова модели
        if _mentions(text_lower, table.get(me, [me.lower()])):
            return None

        # 4. Позвали кого-то другого (а меня — нет) — молчу, без вызова модели
        for other, stems in table.items():
            if other == me:
                continue
            if _mentions(text_lower, stems):
                return {"action": "skip", "reason": f"office-gate: addressed to '{other}'"}

        # 5. Обращение общее — спрашиваем дешёвую модель, моя ли это тема
        role_summary = _load_role_summary()
        if not role_summary:
            return None  # не знаем свою роль — не рискуем промолчать

        if _classify(role_summary, text) is False:
            return {"action": "skip", "reason": "office-gate: not this profile's topic"}
        return None

    except Exception as exc:  # хук не должен ронять доставку сообщения
        logger.warning("office-gate: unexpected error, defaulting to allow: %s", exc)
        return None


def register(ctx) -> None:
    ctx.register_hook("pre_gateway_dispatch", office_gate)
