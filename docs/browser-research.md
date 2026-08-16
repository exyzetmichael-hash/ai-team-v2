# Браузер для research — agent-browser вместо terminal+requests

**Решение принято 2026-08-16**, по итогам недельного `token_audit.py`: `terminal`
оказался на 46% всех вызовов инструментов за неделю. Причина — скилл `research`
(`profiles/edith/skills/research/`) читал страницы через сабагентов, которые при
отсутствии нормального браузерного тула сползали на самодельные python-скрипты
(`requests` + regex/`BeautifulSoup`) через `terminal`. Живьём это уже один раз
падало без установленного `bs4`, и отдельно — сабагент как-то полез парсить
HTML-выдачу Google напрямую вместо использования обычного веб-поиска. Хрупко и
дорого: сырой HTML страницы в контексте — это тысячи лишних токенов на один факт.

## Что изменилось

`browser` — тулсет, уже встроенный в Hermes (`tools/browser_tool.py`), обёртка
над [`agent-browser`](https://github.com/vercel-labs/agent-browser) (Vercel).
Раньше был в `agent.disabled_toolsets` — теперь включён и добавлен в
`platform_toolsets` (и `cli`, и `telegram`, чтобы работал одинаково что из
терминала, что из личного чата).

Ключевое отличие от «просто дать модели скачать HTML»: `agent-browser` работает
через accessibility-дерево, не сырую разметку. `browser_snapshot` отдаёт список
элементов страницы с короткими ref-метками (`@e1`, `@e2`, ...) вместо мегабайта
HTML с версткой, скриптами и футерами — на порядок компактнее, и не нужно писать
regex, чтобы из этого что-то вытащить.

Доступные функции (десять, все уже зарегистрированы в Hermes):
`browser_navigate`, `browser_snapshot`, `browser_click`, `browser_type`,
`browser_scroll`, `browser_back`, `browser_press`, `browser_console`,
`browser_get_images`, `browser_vision`.

Локальный режим — бесплатный: headless Chromium на самом сервере, без API-ключа
и без облачного посредника. В списке провайдеров Hermes он помечен
«★ recommended · free».

Скилл `research` (шаг 2, брифинг сабагенту —
[`skills/research/references/subagent-brief.md`](../profiles/edith/skills/research/references/subagent-brief.md))
обновлён: чтение страниц целиком теперь явно через `browser_navigate` +
`browser_snapshot`, самодельные `requests`-скрипты через `terminal` — запрещены
инструкцией напрямую (это тот шаг назад, который и чинили).

## Разовая настройка на сервере

Нужно один раз установить сам `agent-browser` и его Chromium — сам Hermes
только вызывает уже установленный бинарник, не тянет его сам.

```bash
npm install -g agent-browser
agent-browser install --with-deps   # ставит headless Chromium + системные либы (Debian/Ubuntu)
```

Проверка, что бинарник реально исполняемый (известный баг Hermes #48521 —
после `hermes update` иногда остаётся битый symlink вместо бинарника):

```bash
agent-browser --version
```

Если команда не находится или падает — переустановить: `npm install -g agent-browser --force`.

После установки:

```bash
git pull
cp profiles/edith/config.yaml ~/.hermes/profiles/edith/config.yaml
hermes -p edith gateway restart
```

## Проверка

В личном чате с EDITH — попросить её найти что-то конкретное на сайте, где
обычный веб-поиск даёт только сниппет (например, актуальный тариф на странице
с JS-рендерингом). Если `browser`-тулы работают, ответ придёт с ссылкой и без
задержки на ручной парсинг; если `agent-browser` не установлен на сервере,
вызов инструмента вернёт ошибку — тогда см. «Разовая настройка» выше.

## Известный гочтя: гейтвей не видит уже установленный Chrome (2026-08-16)

Живьём после установки `agent-browser install --with-deps` весь тулсет `browser`
всё равно был недоступен (`check_browser_requirements returned False` в логе
гейтвея), хотя бинарник Chrome реально стоял. Две причины, обе на стороне
Hermes v0.20 + agent-browser 0.27.0, не человеческая ошибка:

1. **Несовпадение нейминга.** `agent-browser install` (0.27.0) качает
   Chrome-for-Testing в `~/.agent-browser/browsers/chrome-<версия>/` — с
   префиксом `chrome-`. Проверка Hermes (`_chromium_installed()` в
   `tools/browser_tool.py`) ищет папки с префиксом `chromium-` (старый формат
   Playwright) — не находит, считает браузер не установленным, гасит весь
   тулсет ещё до попытки запуска.
2. **Sandbox.** Отдельно от (1) — сам Chrome на VPS с Ubuntu 24.04 падает без
   `--no-sandbox` (`No usable sandbox!`, ограничение unprivileged user
   namespaces через AppArmor). У Hermes есть автоподстановка этого флага
   (`_needs_chromium_sandbox_bypass()`), но она добираться не успевает, пока
   не пройдена проверка (1).

**Фикс** — два `Environment=` в `~/.config/systemd/user/hermes-gateway-edith.service`,
явно указывающие путь и не полагающиеся на автоопределение:

```ini
Environment="AGENT_BROWSER_EXECUTABLE_PATH=/home/<юзер>/.agent-browser/browsers/chrome-<версия>/chrome"
Environment="AGENT_BROWSER_ARGS=--no-sandbox,--disable-dev-shm-usage"
```

(путь к бинарнику найти через `find ~/.agent-browser/browsers -maxdepth 3 -type f -perm -u+x`,
взять файл `chrome`, не `chrome-wrapper`/`chrome_sandbox`) — затем
`systemctl --user daemon-reload && systemctl --user restart hermes-gateway-edith`.

Актуально при **любом** пересоздании юнита или переезде на другой сервер —
без этих двух строк `browser` снова молча выключится, и это не будет очевидно
без чтения `journalctl --user -u hermes-gateway-edith | grep browser`.
