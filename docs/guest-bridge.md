# Guest Bridge — EDITH отвечает по @упоминанию из любого чата

Отдельная небольшая служба поверх Telegram Bot API 10.0 (Guest Bots, май 2026),
не патч и не форк Hermes. Разбор всей цепочки рассуждений — в истории проекта:
почему это не решается конфигом Hermes (`guest_mode` там — другая, более старая
вещь, ослабляющая allowlist только для групп, где бот уже состоит участником),
почему сам Hermes пока не умеет `guest_message` (issue [NousResearch/hermes-agent#21587](https://github.com/NousResearch/hermes-agent/issues/21587),
не начато, «no urgency»), и как устроен обход.

## Архитектура

```
Telegram guest_message (бот-двойник EDITH, отдельный токен)
  → guest_bridge.py (свой процесс, getUpdates напрямую к Telegram Bot API)
  → EDITH через /v1/chat/completions (её же gateway-процесс, платформа api_server,
    127.0.0.1:8642 — та же личность, память, инструменты, что и в личке)
  → answerGuestQuery (тем же ботом-двойником)
  → Telegram → тот, кто упомянул бота
```

### Почему бот-двойник, а не тот же токен, что у гейтвея

Первая версия этого моста читала токен основного бота EDITH — и падала в
`409 Conflict` сразу при запуске. Причина архитектурная, не багфиксится:
Telegram разрешает только **одно** активное подключение `getUpdates` на
токен бота одновременно, независимо от `allowed_updates`. Гейтвей EDITH уже
держит этот единственный слот. Хуже — конкурирующий поллинг тем же токеном
может довести retry-логику самого гейтвея (`adapter.py`, счётчик конфликтов
до fatal) до обрыва основного бота, то есть сломать EDITH в личке и группах.

Решение: **отдельный бот** в BotFather с тем же именем и аватаркой, что у
EDITH, но другим юзернеймом — свой токен, свой независимый слот `getUpdates`,
никакого конфликта. Отвечает по-прежнему настоящая EDITH (тот же
`/v1/chat/completions`, та же память и персона) — разница только в том, что
упоминать нужно юзернейм бота-двойника, не основной.

Файлы: [`profiles/edith/guest-bridge/guest_bridge.py`](../profiles/edith/guest-bridge/guest_bridge.py)
(сам мост, подробный разбор рисков и допущений — в docstring файла) +
[`hermes-guest-bridge.service.template`](../profiles/edith/guest-bridge/hermes-guest-bridge.service.template)
(systemd-юнит, отдельный от `hermes-gateway-edith.service` — если мост упадёт,
личка и группы EDITH продолжают работать как обычно).

## ⚠️ Что не проверено до конца

Точная форма `answerGuestQuery` (метод `result: InlineQueryResult`) собрана по
вторичным источникам, не по разделу Methods самой документации — тот не был
под рукой при разборе (core.telegram.org отдаёт 403 из песочницы агента,
открывается из обычного браузера). Если первый живой вызов упадёт — открой
`core.telegram.org/bots/api` → раздел `answerGuestQuery`, пришли точную
сигнатуру, поправим только функцию `_answer_guest_query` в `guest_bridge.py`.

## ⚠️ Первый живой прогон нашёл архитектурную дыру — история

Первый деплой (2026-08-01) поднял `api_server` и systemd-юнит моста
корректно, но мост читал тот же `TELEGRAM_BOT_TOKEN`, что и гейтвей —
результат: `409 Conflict` на `getUpdates` немедленно. Сервис остановлен
до починки. Урок: Telegram-лимит на подключение (не на тип апдейта)
нужно было проверить до написания кода, а не после первого теста. Fix —
раздел «Почему бот-двойник» выше и обновлённые шаги 1/2 ниже.

## ⚠️ Безопасность — почему нельзя пропустить allowlist

`guest_message` приходит от **любого** пользователя Telegram, который написал
`@<юзернейм бота>` в любом чате — это идёт в обход собственного allowlist'а
Hermes (`TELEGRAM_ALLOWED_USERS`), потому что мост говорит с Telegram напрямую,
а не через адаптер Hermes, где та проверка встроена. `guest_bridge.py`
реализует эту проверку заново (та же переменная `TELEGRAM_ALLOWED_USERS`,
поле `guest_bot_caller_user.id` из ответа Telegram) — без неё EDITH отвечала бы
незнакомцам, тратя чужую (твою) квоту OpenRouter и раскрывая контекст личного
ассистента. Не убирать эту проверку и не оставлять `TELEGRAM_ALLOWED_USERS`
пустым.

## Деплой

### 1. Завести бота-двойника и включить ему Guest Mode — это может сделать только Михаил

Программного способа нет — только через интерфейс BotFather:
1. `@BotFather` → `/newbot`.
2. Имя (отображаемое) — то же, что у EDITH, чтобы выглядело одним и тем же
   ассистентом. Юзернейм — обязан быть другим и заканчиваться на `bot`
   (например `edith_guest_bot`, если `edith_aiassist_bot` занят основным).
3. Тем же диалогом с BotFather поставь ту же аватарку, что у основного
   бота EDITH (`/setuserpic`).
4. Найди Mini App с настройками нового бота → включи переключатель
   **Guest Mode**.
5. Сохрани выданный токен — он пойдёт в `GUEST_BOT_TOKEN` на следующем шаге.

Без шага 4 Telegram не будет присылать `guest_message` вообще, сколько
угодно правильно настроенный код на сервере ничего не изменит. Упоминать
в чате нужно будет юзернейм **этого** бота, не `@edith_aiassist_bot`.

### 2. Секреты

```bash
echo "API_SERVER_KEY=$(openssl rand -hex 32)" >> ~/.hermes/profiles/edith/.env
echo "GUEST_BOT_TOKEN=<токен из шага 1>" >> ~/.hermes/profiles/edith/.env
chmod 600 ~/.hermes/profiles/edith/.env
```

### 3. Обновить код и конфиг

```bash
cd ~/ai-team-v2 && git pull
cp profiles/edith/config.yaml ~/.hermes/profiles/edith/config.yaml
hermes -p edith gateway restart
```

Рестарт подхватывает новую платформу `api_server` в её же уже работающем
systemd-сервисе — отдельный процесс поднимать для этого не нужно.

### 4. Проверить, что локальный API живой

```bash
source ~/.hermes/profiles/edith/.env
curl -s http://127.0.0.1:8642/v1/models -H "Authorization: Bearer $API_SERVER_KEY"
```
Должен вернуть список моделей, не ошибку авторизации.

### 5. Поставить сам мост как systemd-сервис

```bash
sed \
  -e "s#/home/michael#$HOME#g" \
  profiles/edith/guest-bridge/hermes-guest-bridge.service.template \
  > /tmp/hermes-guest-bridge.service
mkdir -p ~/.config/systemd/user
mv /tmp/hermes-guest-bridge.service ~/.config/systemd/user/hermes-guest-bridge.service

systemctl --user daemon-reload
systemctl --user enable --now hermes-guest-bridge
systemctl --user status hermes-guest-bridge
```

Смотри логи в реальном времени при первом тесте:
```bash
journalctl --user -u hermes-guest-bridge -f
```

### 6. Проверка

Из **своего** Telegram-аккаунта (id должен быть в `TELEGRAM_ALLOWED_USERS`, иначе
мост молча отклонит — это ожидаемое поведение, не баг) — в любом чате, где
бот-двойник (юзернейм из шага 1) **не** состоит участником:
```
@edith_guest_bot привет, ты меня слышишь?
```
(замени на реальный юзернейм, который выдал BotFather на шаге 1)
Ответ должен прийти в тот же чат одним сообщением. Если тишина — сначала
смотри `journalctl --user -u hermes-guest-bridge -f`, там будет видно, на
каком именно шаге застряло (не пришёл апдейт от Telegram → проверь шаг 1;
`getUpdates`/`answerGuestQuery` вернули ошибку → см. предупреждение про
неподтверждённый формат выше).

## Что это не делает

Один ответ на упоминание, без памяти о переписке в группе — так спроектирован
сам Telegram (privacy-модель guest-ботов: не видит остальные сообщения чата,
не видит участников). Не полноценное присутствие EDITH в группе, а разовые
ответы по вызову — собственно то, что и было целью.
