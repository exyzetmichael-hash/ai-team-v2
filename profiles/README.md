# profiles/ — определение агента

Один профиль Hermes — **EDITH**: единственный ассистент, один Telegram-бот, одна память.

## История

До этого здесь была «офисная» семёрка отдельных профилей-личностей (secretary, brain,
finance, tutor, tracker, research, legal), каждый со своим Telegram-ботом, координацией
через Kanban-доску и плагин `office-gate` (гейт группового чата + арбитраж + межагентная
передача задач). Технически всё это работало — см. `docs/phase-0-verification.md` —
`docs/phase-6-runbook.md` за архивной историей. На практике сам формат «переписываться с
семью коллегами по отдельности» не подошёл: правильная реализация неправильной идеи.
Решение и обсуждение, приведшее к развороту, — в истории этой сессии, не в отдельном файле.

Специализация осталась — просто внутри одного агента через `delegation` (fork/join
сабагенты, `tools/delegate_tool.py`), а не через отдельные боты и Kanban-передачу.

## Раскладка
```
profiles/
└── edith/
    ├── SOUL.md                       # личность — сплав секретаря + второго мозга + финансиста
    ├── MORNING.md                    # чеклист утренней сводки (по cron): календарь, Todoist, таблица финансов
    ├── EVENING.md                    # чеклист вечерней сверки (по cron): что реально сделано, наставничество по учёбе
    ├── config.yaml                   # модель, timezone, terminal (Google Workspace), MCP (todoist)
    └── .env.example                  # шаблон секретов

plugins/
└── budget-guard/                     # мягкое предупреждение о балансе OpenRouter
```

## Доступы
`terminal` — вынужденно (Google Workspace-скилл — Calendar и Sheets — работает только
через shell, `google_api.py`), полноценный шелл, риск смягчён `approvals.mode: manual`.
`code_execution` — выключен по умолчанию (арифметика по деньгам считается вручную с
показом выкладок, см. SOUL.md); включить, если начнёт ошибаться на длинных списках.
`browser`/`computer_use` — выключены (тяжёлая автоматизация не нужна). `delegation` —
**включена** (это и есть механизм внутренней специализации, ключевое отличие от старого
офиса). `web_search`/`web_extract` — доступны всегда.

## Как это попадает на VPS
```bash
hermes profile create edith
cp profiles/edith/SOUL.md         ~/.hermes/profiles/edith/SOUL.md
cp profiles/edith/MORNING.md      ~/.hermes/profiles/edith/MORNING.md
cp profiles/edith/config.yaml     ~/.hermes/profiles/edith/config.yaml
cp -r profiles/edith/skills       ~/.hermes/profiles/edith/skills
cp profiles/edith/.env.example    ~/.hermes/profiles/edith/.env   # затем вписать значения
```
Полная пошаговая инструкция (новый Telegram-бот, Google OAuth с добавленным scope Sheets,
перенос vault-путей от старого brain, финансовая Google-таблица, дашборд) —
`docs/phase-9-edith-runbook.md`.

## Статус
| Профиль | Роль | Статус |
|---|---|---|
| `edith` | единственный ассистент: время/календарь/Todoist, личные финансы + право/учёба/трекинг-проектов/ресёрч как внутренние умения (vault — отдельно у Михаила) | 🚧 строится |

**Маркетплейс WB/Ozon** (~сентябрь) отдельным профилем не будет — Seller API войдёт
инструментами, а экспертиза скиллом, внутрь EDITH. Разбор и проверенные факты (в т.ч. что
ру-прокси не нужен) — [`docs/marketplace-notes.md`](../docs/marketplace-notes.md).
`coder` снят с приоритета — владелец кодит сам с Claude Code.
