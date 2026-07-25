# Фаза 3 — Runbook: второй мозг + vault

**Цель:** агент `brain` читает Obsidian vault по команде и дополняет его **только через PR** — источник правды остаётся у Михаила, агент никогда не пишет в main напрямую.

Как обычно: файлы от меня — в репозитории, выполнение на сервере — твоё.

---

## 1. Подготовить vault-репозиторий

Если vault ещё не в git — заведи **приватный** репозиторий на GitHub, запушь туда текущий vault **со своего компа** (там, где ты его сейчас редактируешь в Obsidian).

Учти сразу: git тут не только «ревью перед записью» — это **механизм синхронизации** между твоим компом и сервером. После того как PR от brain смёржен (на телефоне/сайте), у тебя на компе, чтобы увидеть новую заметку в Obsidian, всего 1-2 команды:
```powershell
cd C:\путь\до\vault
git pull
```
и открыть Obsidian — файл уже там. Ничего сложнее не потребуется.

**На сервере** — ключ **сначала**, клон **потом** (в этом порядке: репозиторий приватный, без ключа с доступом клон вернёт `Repository not found`, даже если репо реально существует). Ключ должен быть **с правом записи** (не read-only, как мы делали для кода): brain будет пушить ветки.
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
(отдельный алиас `github-vault`, чтобы не конфликтовать с read-only ключом от `ai-team-v2`)

Теперь клонируй **через алиас** `github-vault`, не через обычный `github.com` — иначе git попробует дефолтный (read-only, от кодового репозитория) ключ и снова словишь ошибку доступа:
```bash
git clone git@github-vault:<owner>/<vault-repo>.git ~/vault
```

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

**Бюджет-хук — тот же плагин, что у секретаря.** `budget-guard` (фаза 2) привязан к профилю, а не глобален — `brain` тоже тратит OpenRouter-токены, ему нужна своя копия:
```bash
mkdir -p ~/.hermes/profiles/brain/plugins
cp -r ~/ai-team-v2/plugins/budget-guard ~/.hermes/profiles/brain/plugins/
hermes -p brain plugins enable budget-guard
```
Порог можно оставить общим ($3, дефолт) или задать отдельно через `.env` профиля brain — см. `docs/phase-2-runbook.md`, раздел 4.

**Проверь filesystem MCP** — при первом запуске Hermes подтянет `@modelcontextprotocol/server-filesystem` через `npx` (нужен Node.js, он уже стоит из установки Hermes):
```bash
hermes -p brain
```
```
кто ты и чем занимаешься
```

---

## 4. Graphify — граф знаний поверх vault

Решили включить сразу, не откладывать. **Важно понимать, что это такое, прежде чем ставить:** Graphify — не память в смысле «помнит наши разговоры» (это уже бесплатно и из коробки — `MEMORY.md`/`USER.md`, см. раздел 6 ниже). Graphify превращает **содержимое vault** в граф знаний, чтобы brain делал дешёвые структурные запросы (`query_graph`, `get_neighbors` и т.п.) вместо перечитывания сырых файлов на каждый вопрос. Только чтение — писать в vault он не умеет, это по-прежнему через `vault-pr-write` (раздел 5 ниже).

Все команды и флаги ниже сверены по актуальному README проекта (Graphify-Labs/graphify), не угаданы — но живьём я это не гонял (нет своего сервера), так что первый прогон — твоя проверка.

**4.1 Установка** (на сервере, тот же принцип, что был с `uv` раньше — Ubuntu 24.04 блокирует голый `pip install`):
```bash
uv tool install graphifyy
```

**4.2 Не тащить сгенерированный граф в git.** Он пересобирается заново в каждом клоне (у тебя на компе он вообще не нужен — Graphify нужен только brain'у на сервере). В `~/vault/.gitignore`:
```bash
echo "graphify-out/" >> ~/vault/.gitignore
cd ~/vault && git add .gitignore && git commit -m "Ignore generated Graphify output" && git push
```

**4.3 Первая сборка графа.** Индексация markdown (в отличие от кода) идёт через LLM — переиспользуем тот же OpenRouter-ключ, что и у brain, через OpenAI-совместимый режим Graphify:
```bash
cd ~/vault
OPENAI_API_KEY=<твой OPENROUTER_API_KEY> \
OPENAI_BASE_URL=https://openrouter.ai/api/v1 \
OPENAI_MODEL=deepseek/deepseek-v4-flash \
  graphify extract . --backend openai
```
Это стоит реальных, хоть и небольших денег (индексация — не бесплатная операция, в отличие от обычного чтения файлов). Появится `~/vault/graphify-out/graph.json` — это то, что читает `mcp_servers.graphify` в конфиге brain.

**4.4 Автообновление графа при изменениях** (опционально, но рекомендую — иначе граф протухнет после первого же PR):
```bash
cd ~/vault && graphify hook install
```
Ставит git-хуки (`post-commit`/`post-checkout`), которые сами пересобирают граф. ⚠️ **Честно про этот шаг:** хуку тоже нужны те же три переменные (`OPENAI_API_KEY`/`OPENAI_BASE_URL`/`OPENAI_MODEL`) в окружении, где он выполняется — если git-хук триггерится не из твоей интерактивной сессии (например, изнутри действий самого brain через terminal-тул), не факт, что переменные туда долетят. Самое надёжное — прописать эти три строки в `~/.bashrc` на сервере. Если заметишь, что граф не обновляется сам после мержа PR — просто перезапускай `4.3` вручную, это не сломает ничего, кроме актуальности графа (сама запись в vault через PR от этого не зависит).

**4.5 Перезапусти brain**, чтобы подхватил MCP-сервер graphify:
```bash
hermes -p brain gateway restart
```

---

## 5. Тест — то, ради чего всё

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

**Проверь именно то условие, ради которого весь этот план:** после того как смёржил PR (на телефоне или на сайте GitHub), у себя на компе:
```powershell
cd C:\путь\до\vault
git pull
```
и открой Obsidian — заметка, которую написал brain, должна быть уже там. Если пришлось делать что-то ещё — пиши, это не то, что задумывалось.

---

## 6. Память — что уже есть, что добавили, что осталось опционально

**По умолчанию (ничего делать не нужно):** встроенная память Hermes — `MEMORY.md`/`USER.md` внутри профиля brain, отдельная от секретаря. Помнит факты о Михаиле и прошлые разговоры. Работает из коробки, бесплатно.

**Включили в этой фазе — Graphify** (раздел 4 выше): эффективные запросы к **содержимому vault**, не общая память разговоров — это разные вещи, см. раздел 4.

**Осталось опциональным — Honcho (общий факт-слой о владельце между профилями).** То, что в брифе называлось «гибрид»: общая база фактов о Михаиле, доступная и секретарю, и brain, поверх их отдельной личной памяти. Требует внешний сервис (`HONCHO_API_KEY`, honcho.dev) — дополнительная зависимость и, возможно, деньги. Не включаю по умолчанию — сейчас профилей два, острой нужды делиться фактами между ними пока нет; добавим отдельно, если понадобится.

---

## Критерии готовности фазы 3
- [ ] `brain` читает vault и отвечает по существу, не выдумывая.
- [ ] Запись в vault идёт строго через PR — прямых пушей в main нет технически (branch protection) и по факту.
- [ ] SSH-ключ и GitHub-токен brain'а не пересекаются с ключом кодового репозитория `ai-team-v2`.
- [ ] `budget-guard` стоит и у brain, не только у секретаря — расход токенов на оба профиля виден/предупреждается одинаково.
- [ ] После мержа PR заметка появляется локально в Obsidian через `git pull` — без дополнительных шагов.
- [ ] Graphify отвечает на структурные запросы (`query_graph`/`get_neighbors`) — видно по тому, что brain ссылается на связи между заметками, а не только цитирует текст дословно.
