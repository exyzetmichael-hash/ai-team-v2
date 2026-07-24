# Фаза 3 — Runbook: второй мозг + vault

**Цель:** агент `brain` читает Obsidian vault по команде и дополняет его **только через PR** — источник правды остаётся у Михаила, агент никогда не пишет в main напрямую.

Как обычно: файлы от меня — в репозитории, выполнение на сервере — твоё.

---

## 1. Подготовить vault-репозиторий

Если vault ещё не в git — заведи **приватный** репозиторий на GitHub (например `sbrain-wclaude`, как в брифе), запушь туда текущий vault.

**На сервере** — клонируй его в отдельное место (не путать с `~/ai-team-v2`):
```bash
git clone git@github.com:<owner>/<vault-repo>.git ~/vault
```
Про SSH-ключ — на этот раз ключ должен быть **с правом записи** (не read-only, как мы делали для кода): brain будет пушить ветки.
```bash
ssh-keygen -t ed25519 -C "brain-vault" -f ~/.ssh/brain_vault_deploy -N ""
cat ~/.ssh/brain_vault_deploy.pub
```
Добавь на GitHub: `https://github.com/<owner>/<vault-repo>/settings/keys` → Add deploy key → вставь → **включи** "Allow write access" (в этот раз да, в отличие от ключа для кода).
```bash
cat >> ~/.ssh/config <<'EOF'
Host github-vault
  HostName github.com
  IdentityFile ~/.ssh/brain_vault_deploy
  IdentitiesOnly yes
EOF
```
(отдельный алиас `github-vault`, чтобы не конфликтовать с ключом от `ai-team-v2` — если будешь клонировать через этот алиас, используй `git@github-vault:...`)

### Технический предохранитель — защита ветки main
Не полагайся только на инструкцию агенту «не пиши в main». Поставь настоящий барьер на GitHub:
`https://github.com/<owner>/<vault-repo>/settings/branches` → Add rule → ветка `main` → **Require a pull request before merging**. Тогда прямой push в main отклонится технически, даже если что-то пойдёт не так на стороне агента.

---

## 2. GitHub-токен для открытия PR

Отдельно от deploy-ключа (тот умеет только push, не умеет открывать PR через API). Заведи **fine-grained PAT**, ограниченный только этим репозиторием:
`https://github.com/settings/personal-access-tokens/new` → Repository access: **Only select repositories** → выбери vault-репо → Permissions: **Contents: Read and write**, **Pull requests: Read and write**. Скопируй токен.

---

## 3. Профиль `brain`

```bash
hermes profile create brain
cd ~/ai-team-v2 && git pull
cp profiles/brain/SOUL.md      ~/.hermes/profiles/brain/SOUL.md
cp profiles/brain/config.yaml  ~/.hermes/profiles/brain/config.yaml
mkdir -p ~/.hermes/profiles/brain/skills/vault-pr-write
cp profiles/brain/skills/vault-pr-write/SKILL.md ~/.hermes/profiles/brain/skills/vault-pr-write/SKILL.md
```

В `~/.hermes/profiles/brain/config.yaml` поправь пути `/home/michael/vault` на реальный путь клона (если отличается), и в `mcp_servers.filesystem.args` — тоже.

**Секреты** (`~/.hermes/profiles/brain/.env`):
```bash
cat >> ~/.hermes/profiles/brain/.env <<EOF
OBSIDIAN_VAULT_PATH=/home/michael/vault
GITHUB_TOKEN=<PAT из шага 2>
EOF
chmod 600 ~/.hermes/profiles/brain/.env
```

Провайдер — через мастер, как и для секретаря:
```bash
hermes -p brain model
```
OpenRouter → тот же ключ → модель `deepseek/deepseek-v4-flash` (или другую, см. комментарий в config.yaml).

**Проверь filesystem MCP** — при первом запуске Hermes подтянет `@modelcontextprotocol/server-filesystem` через `npx` (нужен Node.js, он уже стоит из установки Hermes):
```bash
hermes -p brain
```
```
кто ты и чем занимаешься
```

---

## 4. Тест — то, ради чего всё

**Чтение:**
```
Что у меня есть в vault про <любая тема, что реально в vault>?
```
Должен найти и процитировать, а не выдумать.

**Запись через PR:**
```
Запиши в vault заметку о том, что мы сегодня подняли второго мозга.
```
Ожидаемо: агент создаёт ветку, коммитит, пушит, открывает PR через `curl`, присылает тебе ссылку. **Проверь на GitHub, что PR реально появился** и что в main ничего не изменилось напрямую.

Если агент попробует писать прямо в main или собьётся на середине git-последовательности — это первое, что стоит откалибровать в `SKILL.md`/`SOUL.md` по факту живого поведения (я не могу это протестировать без сервера и реального vault).

---

## 5. Память — что уже есть и что можно добавить

**По умолчанию (ничего делать не нужно):** встроенная память Hermes — `MEMORY.md`/`USER.md` внутри профиля brain, отдельная от секретаря. Работает из коробки, бесплатно, достаточно для старта.

**Опция А — Honcho (общий факт-слой о владельце между профилями).** Даёт то, что в брифе называлось «гибрид»: общая база фактов о Михаиле, доступная и секретарю, и brain, плюс отдельная личная память каждой роли. Требует внешний сервис (`HONCHO_API_KEY`, honcho.dev) — **дополнительная зависимость и, возможно, деньги**. Не включаю по умолчанию — добавим отдельно, если между профилями реально понадобится делиться фактами (сейчас профилей два, острой нужды пока нет).

**Опция Б — Graphify (экономия токенов на большом vault).** Превращает vault в граф знаний — агент запрашивает граф вместо чтения сырых файлов целиком (заявлено 70-90% меньше токенов на запрос). Поддерживает `.md`/wikilинки — подходит формату Obsidian. У него есть MCP-режим (`python -m graphify.serve`) — конфиг-заглушка уже лежит закомментированной в `profiles/brain/config.yaml`.

⚠️ **Честно про Graphify:** я его не устанавливал и не проверял вживую — только по документации проекта. Два реальных нюанса, прежде чем включать:
1. Для кода индексация локальная (tree-sitter, бесплатно), но **для markdown/заметок — через LLM** (сам сказано в их доках: "docs and media require an LLM backend for semantic extraction") — то есть у Graphify должен быть свой ключ к какой-то модели, это ещё один провайдер в конфиге и ещё какие-то деньги на индексацию.
2. Ставится через `uv tool install graphifyy` (не голый `pip` — Ubuntu 24.04 такое блокирует, мы это уже проходили).

**Мой совет:** не включай Graphify сразу. Начни с обычного filesystem MCP (уже настроен). Возвращайся к Graphify, когда vault реально разрастётся и станет ощутимо, что brain гонит слишком много текста за один запрос — тогда экономия окупит лишнюю деталь в стеке. Если решишь пробовать — раскомментируй блок `graphify` в config.yaml и напиши мне, доведём вместе по факту установки.

---

## Критерии готовности фазы 3
- [ ] `brain` читает vault и отвечает по существу, не выдумывая.
- [ ] Запись в vault идёт строго через PR — прямых пушей в main нет технически (branch protection) и по факту.
- [ ] SSH-ключ и GitHub-токен brain'а не пересекаются с ключом кодового репозитория `ai-team-v2`.
