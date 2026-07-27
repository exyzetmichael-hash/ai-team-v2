#!/usr/bin/env python3
"""Тест распознавания обращений по имени в office-gate.

Зачем отдельный тест: пункты 3-4 гейта (позвали меня / позвали другого)
решают судьбу сообщения БЕЗ обращения к модели — то есть ошибка здесь
означает, что агент молча промолчит или наоборот влезет в чужой разговор,
и никакой классификатор это уже не исправит.

Первая версия ловила основу «мозг» внутри «мозговой штурм» — отсюда
ограничение на длину окончания в `_mentions`. Негативные кейсы ниже
зафиксированы, чтобы это не вернулось.

Запуск (без pytest, зависимостей не нужно):
    python3 plugins/office-gate/test_mentions.py
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import types

# httpx нужен модулю только для сетевого классификатора, который тут не
# вызывается — подсовываем заглушку, чтобы тест работал где угодно.
sys.modules.setdefault("httpx", types.ModuleType("httpx"))

_spec = importlib.util.spec_from_file_location(
    "office_gate", pathlib.Path(__file__).with_name("__init__.py")
)
og = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(og)

# (текст сообщения, кто должен быть распознан)
POSITIVE = [
    ("секретарь, поставь напоминание", "secretary"),
    ("секретарю: перенеси встречу", "secretary"),
    ("секретарями не разбрасываемся", "secretary"),
    ("спроси у юриста про договор", "legal"),
    ("юрист, глянь оферту", "legal"),
    ("мозг, что там в vault", "brain"),
    ("поговори с мозгом об этом", "brain"),
    ("финансист, сколько потратил", "finance"),
    ("что там с финансами", "finance"),
    ("ресёрчер, найди цены", "research"),
    ("исследователь, копни тему", "research"),
    ("тьютор, объясни линал", "tutor"),
    ("трекер, что по fintracker", "tracker"),
    ("brain, посмотри заметки", "brain"),
]

# Никого звать не должны: похожие слова, но обращения нет.
NEGATIVE = [
    "привет, как дела",
    "надо провести мозговой штурм",
    "мозговая активность",
    "финансирование проекта одобрено",
    "юридический адрес компании",
    "что по погоде",
]


def main() -> int:
    table = og._aliases()
    failures = 0

    for text, expected in POSITIVE:
        hits = [p for p, stems in table.items() if og._mentions(text.lower(), stems)]
        if hits != [expected]:
            failures += 1
            print(f"FAIL  {text!r}: ожидался [{expected!r}], получено {hits}")

    for text in NEGATIVE:
        hits = [p for p, stems in table.items() if og._mentions(text.lower(), stems)]
        if hits:
            failures += 1
            print(f"FAIL  {text!r}: ложное срабатывание {hits}")

    total = len(POSITIVE) + len(NEGATIVE)
    if failures:
        print(f"\n{failures} из {total} провалено")
        return 1
    print(f"OK — все {total} проверок прошли")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
