#!/usr/bin/env bash
# Прописывает офисные переменные в .env всех семи профилей.
#
# Использование (на сервере):
#   bash ~/ai-team-v2/scripts/setup-office-env.sh -1002345678901
#
# Где взять ID группы: открой любой топик -> ⋮ -> Copy Link. Придёт ссылка
# вида https://t.me/c/2345678901/3 — ID группы это "-100" + число после /c/,
# то есть -1002345678901. Число после последнего слэша — это id топика, они
# уже зашиты в таблицу ниже.
#
# Скрипт идемпотентный: перед записью удаляет прежние значения тех же ключей,
# поэтому повторный запуск не плодит дубли в .env.

set -euo pipefail

GROUP_ID="${1:-}"
if [[ -z "$GROUP_ID" ]]; then
  echo "Ошибка: не передан ID группы." >&2
  echo "Пример: bash $0 -1002345678901" >&2
  exit 1
fi
if [[ ! "$GROUP_ID" =~ ^-100[0-9]+$ ]]; then
  echo "Ошибка: ID группы должен выглядеть как -100XXXXXXXXXX (получено: $GROUP_ID)" >&2
  exit 1
fi

PROFILES_DIR="$HOME/.hermes/profiles"

# профиль : id топика : username бота (без @)
#
# Топик — куда агент кладёт отчёты о выполненных карточках Kanban, и только
# туда. На обычный разговор это не влияет: в чате агент отвечает в том топике,
# где его спросили.
#
# research сидит в General (1) — отдельного топика под ресёрч нет.
ROWS=(
  "secretary:3:myaisecretary1_bot"
  "finance:4:myaifinansist_bot"
  "tracker:5:trackerpoject_bot"
  "tutor:6:myowntutor_bot"
  "brain:102:sbrainwiki_bot"
  "legal:103:legalllmbot"
  "research:1:autresearcher_bot"
)

KEYS=(
  TELEGRAM_GROUP_ALLOWED_CHATS
  OFFICE_GROUP_CHAT_ID
  OFFICE_GROUP_THREAD_ID
  OFFICE_BOT_USERNAME
  OFFICE_DEFAULT_RESPONDER
  TELEGRAM_HOME_CHANNEL
  TELEGRAM_HOME_CHANNEL_THREAD_ID
)

for row in "${ROWS[@]}"; do
  IFS=":" read -r profile thread bot <<< "$row"
  env_file="$PROFILES_DIR/$profile/.env"

  if [[ ! -d "$PROFILES_DIR/$profile" ]]; then
    echo "!! профиль $profile не найден в $PROFILES_DIR — пропускаю" >&2
    continue
  fi
  touch "$env_file"

  # Убрать прежние значения этих ключей, чтобы повтор не плодил дубли.
  for key in "${KEYS[@]}"; do
    sed -i "/^${key}=/d" "$env_file"
  done

  {
    echo "TELEGRAM_GROUP_ALLOWED_CHATS=$GROUP_ID"
    echo "OFFICE_GROUP_CHAT_ID=$GROUP_ID"
    echo "OFFICE_GROUP_THREAD_ID=$thread"
    echo "OFFICE_BOT_USERNAME=$bot"
    echo "OFFICE_DEFAULT_RESPONDER=secretary"
    # Делает то же самое, что и команда /sethome в чате, но раз и навсегда:
    # без этого каждый профиль при первом сообщении в НОВОЙ для себя сессии
    # спрашивает "No home channel is set" и ждёт /sethome вручную. Ставим
    # ту же переменную, что ставит сама команда (gateway/run.py
    # _home_target_env_var), тем же значением — свой топик группы.
    echo "TELEGRAM_HOME_CHANNEL=$GROUP_ID"
    echo "TELEGRAM_HOME_CHANNEL_THREAD_ID=$thread"
  } >> "$env_file"

  chmod 600 "$env_file"
  echo "$profile: топик $thread, бот @$bot"
done

echo
echo "Готово. Переменные читаются при старте процесса — перезапусти gateway:"
echo "  sudo \$(which hermes) -p secretary gateway restart"
echo "  for p in brain finance tutor tracker research legal; do hermes -p \$p gateway restart; done"
