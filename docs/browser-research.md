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

## Второй гочтя: два фикса выше применены, EDITH всё равно не поднимает браузер (2026-08-19)

После обоих фиксов выше (`chrome-` нейминг + `--no-sandbox` через
`Environment=` в юните) EDITH **всё ещё** отвечала «Chrome не запущен, не
смог его поднять». Причина оказалась не в agent-browser вообще — в двух
независимых вещах:

**2а. Демон agent-browser — отдельный от systemd-юнита долгоживущий процесс,
рестарт юнита его не трогает.** `agent-browser` держит фоновый демон
(`agent-browser-linux-x64`, отдельный PID, не дочерний процесс гейтвея) —
CLI-вызовы из `browser_tool.py` просто подключаются к нему по сокету. Если
демон уже был запущен ДО правки `Environment=` в юните, он навсегда остаётся
со старым (нерабочим) окружением: `agent-browser` прямо отказывается
принимать новые `--args`/env, пока демон жив (`--args ignored: daemon
already running`). Рестарт `hermes-gateway-edith` эту проблему не решает —
демон переживает рестарт гейтвея. Нужно убить его явно, чтобы следующий
вызов поднял новый демон уже с правильным окружением:

```bash
agent-browser close --all   # или pkill -f agent-browser-linux-x64
```

(сам факт «когда правил юнит — не тронул демон» и потерял ~30 минут на этом:
`ps aux | grep agent-browser` сразу показал бы процесс, живущий с момента
ДО правки юнита).

**2б. Более важная причина — `browser` в этой версии Hermes это не всегда
agent-browser.** В `tools/browser_use_cli.py` появился альтернативный бэкенд
— CLI-обвязка над Python-пакетом `browser-use` (не путать с `agent-browser`
от Vercel — это два разных инструмента с почти одинаковым названием).
`is_browser_use_cli_mode()`:

> Browser Use mode is the DEFAULT: an unset `browser.backend` ("") enables
> it whenever the browser-use CLI is runnable (installed binary or uvx).

То есть если `browser.backend` не задан явно в конфиге — а его никто и не
задавал, потому что весь этот раздел документа был написан про
agent-browser, — и на сервере стоит `uvx` (стоит, часть `uv`-тулчейна для
других задач), Hermes **молча подменяет весь тулсет** `browser_navigate`/
`browser_snapshot`/... на один инструмент `browser_exec`, работающий через
`browser-use`. Этот харнесс сам пытается подключиться по CDP к уже
запущенному локальному Chrome, а не поднимает свой через
`AGENT_BROWSER_EXECUTABLE_PATH` — поэтому все переменные окружения из
раздела «Известный гочтя» выше не имели никакого эффекта: EDITH их вызывала,
но не тем инструментом. Ошибка в логе харнесса (не в логе гейтвея, отдельный
файл — `~/.hermes/profiles/edith/home/.config/browser-harness/tmp/bu-default.log`):

```
fatal: chrome-not-running: no supported Chromium-family browser is running -- start Chrome, then retry
```

**Фикс** — явно выключить автопереход на `browser-use`, оставив
agent-browser, ради которого и писался весь этот документ:

```yaml
# ~/.hermes/profiles/edith/config.yaml
browser:
  backend: 'off'
```

(закавычено намеренно — YAML 1.1 без кавычек читает `off` как булево
`False`; код `get_browser_backend()` это отдельно обрабатывает, но кавычки
оставлены для консистентности с остальным конфигом, где та же ловушка уже
описана для `display.memory_notifications`.)

После `daemon-reload` + `restart hermes-gateway-edith` живой прогон
(CLI, `hermes -p edith -z "..."`) подтвердил: `browser_navigate` +
`browser_snapshot` реально ходят на страницу и возвращают настоящий
accessibility snapshot — WB (`seller.wildberries.ru/tariffs/commissions`)
честно упёрся в форму логина продавца, Ozon (несколько публичных URL) — в
антибот; оба раза EDITH отказалась подставить цифру по памяти вместо
непрочитанной страницы. Значит сам браузерный тул уже рабочий; доступ к
конкретно комиссиям FBO без входа в кабинет продавца — отдельный вопрос, не
про Hermes.

**Порядок диагностики на будущее**, если `browser` снова "не поднимается"
после вроде бы верного фикса:

1. `cat /proc/$(systemctl --user show -p MainPID --value hermes-gateway-edith)/environ | grep AGENT_BROWSER` — юнит вообще видит переменные?
2. `ps aux | grep agent-browser` — нет ли демона, живущего ДО последней правки юнита? Если есть — `agent-browser close --all`.
3. `cat ~/.hermes/profiles/edith/config.yaml | grep -A2 '^browser:'` — не откатился ли `backend: off` (например, после `cp` из репозитория, где этой строки может не быть).
4. Лог реальной ошибки harness'а лежит не в `gateway.log`, а в
   `~/.hermes/profiles/edith/home/.config/browser-harness/tmp/bu-default.log`
   — если тулсет `browser-use`, а не `agent-browser`, снова стал активным
   (например, после обновления Hermes сбросило `browser.backend`), ошибка
   там, не в основных логах гейтвея.
