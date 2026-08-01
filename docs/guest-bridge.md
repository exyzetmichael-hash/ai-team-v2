# Guest Bridge — EDITH отвечает по @упоминанию из любого чата

Отдельная небольшая служба поверх Telegram Bot API 10.0 (Guest Bots, май 2026),
не патч и не форк Hermes. Разбор всей цепочки рассуждений — в истории проекта:
почему это не решается конфигом Hermes (`guest_mode` там — другая, более старая
вещь, ослабляющая allowlist только для групп, где бот уже состоит участником),
почему сам Hermes пока не умеет `guest_message` (issue [NousResearch/hermes-agent#21587](https://github.com/NousResearch/hermes-agent/issues/21587),
не начато, «no urgency»), и как устроен обход.

## Архитектура

```
Telegram guest_message
  → guest_bridge.py (свой процесс, getUpdates напрямую к Telegram Bot API)
  → EDITH через /v1/chat/completions (её же gateway-процесс, платформа api_server,
    127.0.0.1:8642 — та же личность, память, инструменты, что и в личке)
  → answerGuestQuery
  → Telegram → тот, кто упомянул бота
```

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

### 1. Включить Guest Mode в BotFather — это может сделать только Михаил

Программного способа нет — только через интерфейс:
1. Открой `@BotFather` в Telegram.
2. Найди Mini App с настройками `@edith_aiassist_bot`.
3. Включи переключатель **Guest Mode**.

Без этого шага Telegram не будет присылать `guest_message` вообще, сколько
угодно правильно настроенный код на сервере ничего не изменит.

### 2. Секрет для локального API

```bash
echo "API_SERVER_KEY=$(openssl rand -hex 32)" >> ~/.hermes/profiles/edith/.env
chmod 600 ~/.hermes/profiles/edith/.env
```

### 3. Обновить код и конфиг

```bash
cd ~/ai-team-v2 && git pull
cp profiles/edith/config.yaml ~/.hermes/profiles/edith/config.yaml
cp -r profiles/edith/guest-bridge ~/ai-team-v2/profiles/edith/guest-bridge  # уже там после git pull
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
`@edith_aiassist_bot` **не** состоит участником:
```
@edith_aiassist_bot привет, ты меня слышишь?
```
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
