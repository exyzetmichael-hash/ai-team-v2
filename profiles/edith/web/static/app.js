'use strict';

// Токен лежит И в localStorage, И в cookie на год. Михаил про то, что убивает
// инструмент: «логин каждый раз это вообще пиздец». localStorage у PWA на
// домашнем экране иногда живёт отдельно от вкладки браузера и чистится
// агрессивнее — cookie это подстраховывает. Экран входа он должен увидеть
// ровно один раз за устройство.
const TOKEN_KEY = 'edith_ui_token';

function saveToken(t) {
  try { localStorage.setItem(TOKEN_KEY, t); } catch {}
  document.cookie = `${TOKEN_KEY}=${encodeURIComponent(t)}; path=/; max-age=31536000; SameSite=Lax`;
}
function loadToken() {
  try {
    const v = localStorage.getItem(TOKEN_KEY);
    if (v) return v;
  } catch {}
  const m = document.cookie.match(new RegExp(`(?:^|; )${TOKEN_KEY}=([^;]*)`));
  return m ? decodeURIComponent(m[1]) : '';
}
function clearToken() {
  try { localStorage.removeItem(TOKEN_KEY); } catch {}
  document.cookie = `${TOKEN_KEY}=; path=/; max-age=0`;
}

const HOME_CACHE_KEY = 'edith_home_snapshot';

let token = loadToken();
let currentConv = null;
let streaming = false;
let recorder = null;
let recordChunks = [];

const $ = (id) => document.getElementById(id);

// ---------------------------------------------------------------- разметка

/**
 * Минимальный markdown → HTML.
 *
 * Свой, а не библиотека: страница должна работать без интернета и без
 * CDN (сервер в tailnet, у телефона может не быть выхода наружу), а
 * тащить бандл ради жирного текста и таблиц — перебор. Покрыто то, чем
 * EDITH реально пользуется: заголовки, списки, код, таблицы, цитаты.
 *
 * ВАЖНО: экранирование идёт ПЕРВЫМ шагом, до любых замен. Ответ модели —
 * это в том числе содержимое веб-страниц, которые она читала браузером,
 * то есть текст из интернета. Без экранирования любой прочитанный ею
 * <script> исполнился бы здесь.
 */
function renderMarkdown(src) {
  let s = src
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');

  const blocks = [];
  s = s.replace(/```(\w*)\n?([\s\S]*?)```/g, (_, lang, code) => {
    blocks.push(`<pre><code>${code.replace(/\n$/, '')}</code></pre>`);
    return `\n\nBLOCK${blocks.length - 1}\n\n`;
  });

  s = s.replace(/`([^`\n]+)`/g, '<code>$1</code>');
  s = s.replace(/^###\s+(.+)$/gm, '<h3>$1</h3>');
  s = s.replace(/^##\s+(.+)$/gm, '<h2>$1</h2>');
  s = s.replace(/^#\s+(.+)$/gm, '<h1>$1</h1>');
  s = s.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/(^|[\s(])\*([^*\n]+)\*/g, '$1<em>$2</em>');
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
    '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  s = s.replace(/^&gt;\s?(.*)$/gm, '<blockquote>$1</blockquote>');

  s = renderTables(s);

  s = s.replace(/(?:^[-*]\s+.+$\n?)+/gm, (block) => {
    const items = block.trim().split('\n')
      .map((l) => `<li>${l.replace(/^[-*]\s+/, '')}</li>`).join('');
    return `<ul>${items}</ul>`;
  });
  s = s.replace(/(?:^\d+[.)]\s+.+$\n?)+/gm, (block) => {
    const items = block.trim().split('\n')
      .map((l) => `<li>${l.replace(/^\d+[.)]\s+/, '')}</li>`).join('');
    return `<ol>${items}</ol>`;
  });

  s = s.split(/\n{2,}/).map((chunk) => {
    const t = chunk.trim();
    if (!t) return '';
    if (/^(?:<(?:h[1-3]|ul|ol|pre|table|blockquote)|BLOCK\d+$)/.test(t)) return t;
    return `<p>${t.replace(/\n/g, '<br>')}</p>`;
  }).join('');

  return s.replace(/BLOCK(\d+)/g, (_, i) => blocks[Number(i)]);
}

function renderTables(s) {
  const lines = s.split('\n');
  const out = [];
  for (let i = 0; i < lines.length; i++) {
    const isRow = (l) => /^\s*\|.*\|\s*$/.test(l || '');
    const isSep = (l) => /^\s*\|[\s:|-]+\|\s*$/.test(l || '');
    if (isRow(lines[i]) && isSep(lines[i + 1])) {
      const cells = (l) => l.trim().replace(/^\||\|$/g, '').split('|').map((c) => c.trim());
      const head = cells(lines[i]);
      let j = i + 2;
      const body = [];
      while (j < lines.length && isRow(lines[j])) { body.push(cells(lines[j])); j++; }
      out.push(
        '<table><thead><tr>' + head.map((c) => `<th>${c}</th>`).join('') + '</tr></thead><tbody>' +
        body.map((r) => '<tr>' + r.map((c) => `<td>${c}</td>`).join('') + '</tr>').join('') +
        '</tbody></table>'
      );
      i = j - 1;
    } else {
      out.push(lines[i]);
    }
  }
  return out.join('\n');
}

// -------------------------------------------------------------- приветствие

/**
 * Приветствие считается ЗДЕСЬ, по часам телефона, а не на сервере.
 *
 * Сервер живёт в CEST, профиль EDITH до сих пор в Asia/Krasnoyarsk, Михаил
 * переезжает в Питер. Единственные часы, которым можно верить, — те, что у
 * него в руке. Иначе приложение желало бы доброго утра в четыре ночи, как
 * это уже грозит крону после переезда.
 *
 * Без вызова модели: экран открывается по десять раз в день, платить за
 * строчку приветствия каждый раз — то самое расточительство, которое мы
 * весь август вычищали.
 */
const GREETINGS = {
  // ⚠️ Никаких конкретных часов в тексте («три часа ночи»): вариант
  // выбирается на весь диапазон 0–5, и в четыре утра такая фраза врёт.
  night:   ['Ты ещё не спишь', 'Глубокая ночь', 'Ночь на дворе', 'Не спится?'],
  early:   ['Рано поднялся', 'Доброе утро', 'Ещё только светает', 'Раненько'],
  morning: ['Доброе утро', 'С добрым утром', 'Утро', 'Ну что, поехали'],
  day:     ['Добрый день', 'Как день', 'День в разгаре', 'Привет'],
  evening: ['Добрый вечер', 'Вечер', 'Как прошёл день', 'Привет'],
  late:    ['Поздний вечер', 'Пора закругляться', 'Уже поздно', 'Добрый вечер'],
};

function timeBucket(h) {
  if (h < 5) return 'night';
  if (h < 8) return 'early';
  if (h < 12) return 'morning';
  if (h < 17) return 'day';
  if (h < 22) return 'evening';
  return 'late';
}

function pickGreeting() {
  const now = new Date();
  const options = GREETINGS[timeBucket(now.getHours())];
  // Меняется от открытия к открытию, но не дёргается при перерисовке
  // внутри одной минуты.
  const seed = Math.floor(now.getTime() / 60000);
  return options[seed % options.length];
}

function eventTime(iso, allDay) {
  if (allDay) return 'весь день';
  const d = new Date(iso);
  if (isNaN(d)) return '';
  return d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
}

function buildSubline(data) {
  const bits = [];
  const events = (data.events || []).filter((e) => !e.all_day && new Date(e.start) > new Date());
  if (events.length) {
    bits.push(`ближайшее в ${eventTime(events[0].start, false)} — ${events[0].summary}`);
  }
  const overdue = (data.tasks || []).filter((t) => t.overdue).length;
  const total = (data.tasks || []).length;
  if (overdue) bits.push(`просрочено ${overdue}`);
  else if (total) bits.push(`задач на сегодня: ${total}`);
  return bits.join(', ');
}

// --------------------------------------------------------------- главный экран

function money(n) {
  return Math.round(n).toLocaleString('ru-RU') + ' ₽';
}

function renderStats(data) {
  const m = data && data.money;
  const mail = data && data.mail;

  const moneyCard = $('money-card');
  if (m) {
    $('money-value').textContent = money(m.spent_month);
    let sub = 'потрачено в этом месяце';
    let tone = 'ok';
    if (m.over_limit && m.over_limit.length) {
      sub = `превышен лимит: ${m.over_limit.join(', ')}`;
      tone = 'danger';
    } else if (m.near_limit && m.near_limit.length) {
      sub = `близко к лимиту: ${m.near_limit.join(', ')}`;
      tone = 'warn';
    }
    $('money-sub').textContent = sub;
    moneyCard.classList.remove('hidden', 'tone-ok', 'tone-warn', 'tone-danger');
    moneyCard.classList.add(`tone-${tone}`);
    moneyCard.onclick = () => { openChat(); $('input').value = 'Как у меня с тратами в этом месяце? '; $('input').focus(); };
  } else {
    moneyCard.classList.add('hidden');
  }

  const mailCard = $('mail-card');
  if (mail && mail.unread > 0) {
    $('mail-value').textContent = mail.unread;
    mailCard.classList.remove('hidden');
    mailCard.onclick = () => { openChat(); $('input').value = 'Что у меня непрочитанного в почте? '; $('input').focus(); };
  } else {
    mailCard.classList.add('hidden');
  }

  $('stats-block').classList.toggle('hidden', !(m || (mail && mail.unread > 0)));
}

function renderHome(data) {
  $('greeting-line').textContent = pickGreeting();
  $('greeting-sub').textContent = data ? buildSubline(data) : '';

  renderStats(data);

  const events = (data && data.events) || [];
  const tasks = (data && data.tasks) || [];

  const evList = $('events-list');
  evList.innerHTML = '';
  events.slice(0, 5).forEach((e) => {
    const row = document.createElement('div');
    row.className = 'row';
    const time = document.createElement('span');
    time.className = 'row-time';
    time.textContent = eventTime(e.start, e.all_day);
    const text = document.createElement('span');
    text.className = 'row-text';
    text.textContent = e.summary + (e.location ? ` · ${e.location}` : '');
    row.append(time, text);
    evList.appendChild(row);
  });
  $('events-block').classList.toggle('hidden', events.length === 0);

  const taskList = $('tasks-list');
  taskList.innerHTML = '';
  tasks.slice(0, 6).forEach((t) => {
    const row = document.createElement('div');
    row.className = 'row' + (t.overdue ? ' overdue' : '');
    const dot = document.createElement('span');
    dot.className = 'row-dot';
    const text = document.createElement('span');
    text.className = 'row-text';
    text.textContent = t.content;
    row.append(dot, text);
    // Тап по задаче — сразу вопрос про неё, без печатания.
    row.onclick = () => { openChat(); $('input').value = `Про задачу «${t.content}»: `; $('input').focus(); };
    taskList.appendChild(row);
  });
  $('tasks-block').classList.toggle('hidden', tasks.length === 0);

  const hasStats = !$('stats-block').classList.contains('hidden');
  $('home-empty').classList.toggle('hidden', events.length > 0 || tasks.length > 0 || hasStats);
}

async function loadHome() {
  // Сначала — мгновенно из снимка. Экран не ждёт сеть никогда.
  let cached = null;
  try { cached = JSON.parse(localStorage.getItem(HOME_CACHE_KEY) || 'null'); } catch {}
  renderHome(cached);

  try {
    const res = await api('/api/home');
    const data = await res.json();
    try { localStorage.setItem(HOME_CACHE_KEY, JSON.stringify(data)); } catch {}
    renderHome(data);
  } catch (err) {
    console.error(err);
  }
}

function openHome() {
  $('home').classList.remove('hidden');
  $('messages').classList.add('hidden');
  $('conv-title').textContent = 'EDITH';
  currentConv = null;
  closeSidebar();
  loadHome();
}

function openChat() {
  $('home').classList.add('hidden');
  $('messages').classList.remove('hidden');
}

// ------------------------------------------------------------------- сеть

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: { 'X-UI-Token': token, ...(options.headers || {}) },
  });
  if (res.status === 401) {
    clearToken();
    location.reload();
    throw new Error('unauthorized');
  }
  return res;
}

// --------------------------------------------------------------- сообщения

function addMessage(role, content) {
  const wrap = document.createElement('div');
  wrap.className = `msg ${role}`;
  const body = document.createElement('div');
  body.className = 'msg-body';
  if (role === 'assistant') body.innerHTML = renderMarkdown(content);
  else body.textContent = content;
  wrap.appendChild(body);
  $('messages').appendChild(wrap);
  scrollDown();
  return body;
}

function scrollDown() {
  const m = $('messages');
  m.scrollTop = m.scrollHeight;
}

async function loadConversations() {
  const res = await api('/api/conversations');
  const { conversations } = await res.json();
  const list = $('conv-list');
  list.innerHTML = '';
  conversations.forEach((c) => {
    const item = document.createElement('div');
    item.className = 'conv-item' + (c.id === currentConv ? ' active' : '');

    const title = document.createElement('span');
    title.className = 'conv-item-title';
    title.textContent = c.title;
    title.onclick = () => openConversation(c.id, c.title);

    const del = document.createElement('button');
    del.className = 'conv-del';
    del.textContent = '×';
    del.setAttribute('aria-label', 'Удалить разговор');
    del.onclick = async (e) => {
      e.stopPropagation();
      if (!confirm(`Удалить «${c.title}»?`)) return;
      await api(`/api/conversations/${c.id}`, { method: 'DELETE' });
      if (currentConv === c.id) openHome();
      loadConversations();
    };

    item.append(title, del);
    list.appendChild(item);
  });
}

async function openConversation(id, title) {
  currentConv = id;
  $('conv-title').textContent = title || 'EDITH';
  openChat();
  closeSidebar();
  const res = await api(`/api/conversations/${id}/messages`);
  const { messages } = await res.json();
  $('messages').innerHTML = '';
  messages.forEach((m) => addMessage(m.role, m.content));
  loadConversations();
}

function openSidebar() { $('sidebar').classList.add('open'); $('scrim').classList.remove('hidden'); }
function closeSidebar() { $('sidebar').classList.remove('open'); $('scrim').classList.add('hidden'); }

// ------------------------------------------------------------------- чат

async function send(text) {
  if (streaming) return;
  streaming = true;
  $('send').disabled = true;
  openChat();

  addMessage('user', text);

  const body = addMessage('assistant', '');
  // Живой статус: что делает и сколько уже думает. Михаил про долгие паузы —
  // «ощущение, что она тупо хуйнёй страдает и тратит мои токены». Секундомер
  // отвечает на это честно, не выдумывая занятий, которых не было.
  const started = Date.now();
  let statusText = 'думает';
  let answer = '';
  const renderStatus = () => {
    const sec = Math.round((Date.now() - started) / 1000);
    body.innerHTML = `<div class="status"><span class="dots"><span></span><span></span><span></span></span>`
      + `<span class="status-text"></span></div>`;
    body.querySelector('.status-text').textContent = `${statusText} · ${sec}с`;
  };
  renderStatus();
  const ticker = setInterval(() => { if (!answer) renderStatus(); }, 1000);

  try {
    const res = await api('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text, conversation_id: currentConv }),
    });

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      const parts = buffer.split('\n\n');
      buffer = parts.pop();
      for (const part of parts) {
        const evMatch = part.match(/^event:\s*(.+)$/m);
        const dataMatch = part.match(/^data:\s*(.+)$/m);
        if (!evMatch || !dataMatch) continue;
        let payload;
        try { payload = JSON.parse(dataMatch[1]); } catch { continue; }

        if (evMatch[1] === 'meta' && payload.conversation_id) {
          const isNew = currentConv !== payload.conversation_id;
          currentConv = payload.conversation_id;
          if (isNew) loadConversations();
        } else if (evMatch[1] === 'status') {
          statusText = payload.text;
          if (!answer) renderStatus();
        } else if (evMatch[1] === 'delta') {
          answer += payload.text;
          body.innerHTML = renderMarkdown(answer);
          scrollDown();
        }
      }
    }
  } catch (err) {
    console.error(err);
    if (!answer) body.innerHTML = '<p>Связь оборвалась. Ответ мог сохраниться — обнови страницу.</p>';
  } finally {
    clearInterval(ticker);
    streaming = false;
    $('send').disabled = false;
    if (!answer && !body.textContent.trim()) body.innerHTML = '<p>Пустой ответ.</p>';
    loadConversations();
  }
}

// ------------------------------------------------------------------- голос

/**
 * Запись голоса → Groq Whisper на сервере → текст в поле ввода.
 *
 * Не браузерное распознавание (Web Speech API): оно шлёт звук в Google, по-
 * русски заметно хуже и в Firefox отсутствует. Groq у Михаила уже подключён
 * как STT для голосовых в Telegram — тот же ключ.
 *
 * Текст попадает в поле, а НЕ отправляется сразу: распознавание иногда врёт,
 * и увидеть это до отправки дешевле, чем получить ответ не на тот вопрос.
 *
 * ⚠️ Микрофон требует защищённого контекста: по обычному http:// на
 * tailnet-адресе navigator.mediaDevices просто отсутствует. Поэтому сервис
 * выставляется через `tailscale serve` (HTTPS с сертификатом на tailnet-
 * домен) — см. docs/web-ui.md.
 */
function micAvailable() {
  return !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder);
}

async function toggleRecording() {
  if (!micAvailable()) {
    alert('Микрофон недоступен: страница открыта не по HTTPS.\n\n' +
          'Открой адрес вида https://<имя-машины>.<tailnet>.ts.net — см. docs/web-ui.md.');
    return;
  }

  if (recorder && recorder.state === 'recording') {
    recorder.stop();
    return;
  }

  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (err) {
    alert('Не дали доступ к микрофону.');
    return;
  }

  recordChunks = [];
  recorder = new MediaRecorder(stream);
  recorder.ondataavailable = (e) => { if (e.data.size) recordChunks.push(e.data); };
  recorder.onstop = async () => {
    stream.getTracks().forEach((t) => t.stop());
    $('mic').classList.remove('recording');
    const blob = new Blob(recordChunks, { type: recorder.mimeType || 'audio/webm' });
    if (blob.size < 1000) return;  // нажал и сразу отпустил — тишина

    $('mic').classList.add('busy');
    try {
      const res = await api('/api/stt', {
        method: 'POST',
        headers: { 'Content-Type': blob.type },
        body: blob,
      });
      const data = await res.json();
      if (data.text) {
        const input = $('input');
        input.value = (input.value ? input.value + ' ' : '') + data.text;
        input.dispatchEvent(new Event('input'));
        input.focus();
      } else {
        alert(data.error || 'Не разобрала.');
      }
    } catch (err) {
      console.error(err);
      alert('Распознавание не удалось.');
    } finally {
      $('mic').classList.remove('busy');
    }
  };
  recorder.start();
  $('mic').classList.add('recording');
}

// ------------------------------------------------------------------- старт

function initApp() {
  $('gate').classList.add('hidden');
  $('app').classList.remove('hidden');
  openHome();
  loadConversations();

  const input = $('input');

  input.addEventListener('input', () => {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 180) + 'px';
  });

  // Enter отправляет, Shift+Enter — перенос. На телефоне Enter всегда
  // переносит: там это единственный способ написать абзац, а отправка —
  // кнопка под большим пальцем.
  input.addEventListener('keydown', (e) => {
    const isMobile = window.matchMedia('(max-width: 760px)').matches;
    if (e.key === 'Enter' && !e.shiftKey && !isMobile) {
      e.preventDefault();
      $('composer').requestSubmit();
    }
  });

  $('composer').addEventListener('submit', (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (!text || streaming) return;
    input.value = '';
    input.style.height = 'auto';
    send(text);
  });

  $('mic').onclick = toggleRecording;
  if (!micAvailable()) $('mic').classList.add('unavailable');

  $('new-chat').onclick = openHome;
  $('home-btn').onclick = openHome;
  $('open-sidebar').onclick = openSidebar;
  $('close-sidebar').onclick = closeSidebar;
  $('scrim').onclick = closeSidebar;

  // Вернулся во вкладку — обновить главный экран, если он открыт.
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && !$('home').classList.contains('hidden')) loadHome();
  });
}

async function tryToken(candidate) {
  try {
    const res = await fetch('/api/conversations', { headers: { 'X-UI-Token': candidate } });
    return res.ok;
  } catch {
    return false;
  }
}

(async function boot() {
  if (token && await tryToken(token)) {
    saveToken(token);  // продлеваем cookie на год при каждом заходе
    initApp();
    return;
  }
  clearToken();
  $('gate').classList.remove('hidden');

  const submit = async () => {
    const candidate = $('token-input').value.trim();
    if (!candidate) return;
    if (await tryToken(candidate)) {
      token = candidate;
      saveToken(candidate);
      initApp();
    } else {
      $('gate-error').textContent = 'Неверный токен';
    }
  };

  $('token-submit').onclick = submit;
  $('token-input').addEventListener('keydown', (e) => { if (e.key === 'Enter') submit(); });
})();
