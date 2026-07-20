# profiles/ — определения агентов

Каждый агент = отдельный **профиль** Hermes (свой дом, память, конфиг, Telegram-бот).
Здесь лежит версионируемая часть профиля: характер и несекретные настройки.
Секреты (ключи, токены) в git не хранятся — только шаблон `.env.example`.

## Раскладка
```
profiles/
├── .env.example          # шаблон секретов (копируется в дом профиля как .env)
└── secretary/
    ├── SOUL.md           # личность и инструкции секретаря
    └── config.yaml       # модель + выключенные тулсеты (без секретов)
```

## Как это попадает на VPS
На VPS профиль живёт в `~/.hermes/profiles/<name>/`. Деплой профиля:
```bash
hermes profile create <name>
cp profiles/<name>/SOUL.md    ~/.hermes/profiles/<name>/SOUL.md
cp profiles/<name>/config.yaml ~/.hermes/profiles/<name>/config.yaml
cp profiles/.env.example       ~/.hermes/profiles/<name>/.env   # затем вписать значения
```
Подробные шаги для секретаря — в `docs/phase-1-runbook.md`.

## Статус агентов
| Профиль | Роль | Статус |
|---|---|---|
| `secretary` | дедлайны, напоминания, календарь, сводки | в работе (фаза 1) |
| `brain` | второй мозг, vault через PR | далее |
| `coder` | код, репозитории, PR | позже |
| `finance`, `market`, `research` | по мере надобности | позже |
