# profiles/ — определения агентов

Каждый агент = отдельный **профиль** Hermes (свой дом, память, конфиг, Telegram-бот).
Здесь лежит версионируемая часть профиля: характер и несекретные настройки.
Секреты (ключи, токены) в git не хранятся — только шаблоны `.env.example` (у каждого
профиля свой, набор секретов разный: у секретаря Telegram-токен, у brain — GitHub PAT
и путь к vault).

## Раскладка
```
profiles/
├── .env.example              # шаблон секретов ИМЕННО для secretary (не общий!)
├── secretary/
│   ├── SOUL.md               # личность и инструкции секретаря
│   ├── MORNING.md            # чеклист утренней сводки (по cron, см. фаза 2)
│   └── config.yaml           # модель + выключенные тулсеты (без секретов)
└── brain/
    ├── SOUL.md                       # личность второго мозга
    ├── config.yaml                   # модель, filesystem-MCP на vault, terminal.cwd
    ├── .env.example                  # шаблон секретов ИМЕННО для brain
    └── skills/vault-pr-write/SKILL.md  # запись в vault только через PR

plugins/                       # общие для всех профилей, ставятся выборочно
├── budget-guard/               # мягкое предупреждение о балансе OpenRouter (фаза 2, у обоих)
└── office-gate/                # дешёвый гейт группового чата (фаза 5, черновик)
```

## Как это попадает на VPS
На VPS профиль живёт в `~/.hermes/profiles/<name>/`. Общая форма (секреты — из шаблона
именно этого профиля, не из чужого):
```bash
hermes profile create <name>
cp profiles/<name>/SOUL.md    ~/.hermes/profiles/<name>/SOUL.md
cp profiles/<name>/config.yaml ~/.hermes/profiles/<name>/config.yaml
cp profiles/<name>/.env.example ~/.hermes/profiles/<name>/.env   # затем вписать значения
```
Подробные пошаговые инструкции — по фазам, не здесь: секретарь — `docs/phase-1-runbook.md`
(база) и `docs/phase-2-runbook.md` (календарь/сводки/бюджет); brain — `docs/phase-3-runbook.md`;
офис (Kanban + office-gate) — `docs/phase-5-runbook.md`; веб-доступ — `docs/phase-6-runbook.md`.

## Статус агентов
| Профиль | Роль | Статус |
|---|---|---|
| `secretary` | дедлайны, напоминания, календарь, сводки | в работе (фаза 1), артефакты фазы 2 готовы в репо |
| `brain` | второй мозг, vault через PR | артефакты фазы 3 готовы в репо, не развёрнут |
| `coder` | код, репозитории, PR | снято с приоритета — владелец кодит сам с Claude Code |
| `market` | маркетплейсер WB/Ozon | главный бизнес-приоритет, фаза 4, старт ~сентябрь, не начато — тестить пока негде |
| `finance`, `research` | по мере надобности | позже, опционально |
