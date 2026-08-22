'use strict';

// Токен интерфейса лежит в localStorage, чтобы не вводить его каждый раз с
// телефона. Это осознанный размен: устройство уже в tailnet, то есть своё;
// защищаемся от чужих рук на своём телефоне, а не от сети.
const TOKEN_KEY = 'edith_ui_token';

let token = localStorage.getItem(TOKEN_KEY) || '';
let currentConv = null;
let streaming = false;

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
  // Блоки кода прячем целиком, чтобы внутренние символы не поймались
  // остальными правилами.
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

  // Списки: собираем подряд идущие пункты в один <ul>/<ol>.
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

  // Остаток разбиваем на абзацы, не трогая уже готовые блоки.
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

// ------------------------------------------------------------------- сеть

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: { 'Content-Type': 'application/json', 'X-UI-Token': token, ...(options.headers || {}) },
  });
  if (res.status === 401) {
    localStorage.removeItem(TOKEN_KEY);
    location.reload();
    throw new Error('unauthorized');
  }
  return res;
}

// --------------------------------------------------------------- интерфейс

function addMessage(role, content) {
  const wrap = document.createElement('div');
  wrap.className = `msg ${role}`;
  const body = document.createElement('div');
  body.className = 'msg-body';
  if (role === 'assistant') {
    body.innerHTML = renderMarkdown(content);
  } else {
    body.textContent = content;
  }
  wrap.appendChild(body);
  $('messages').appendChild(wrap);
  scrollDown();
  return body;
}

function scrollDown() {
  const m = $('messages');
  m.scrollTop = m.scrollHeight;
}

function showEmptyState() {
  $('messages').innerHTML =
    '<div class="empty-state">Спроси что-нибудь.<br>Это та же EDITH, что в Telegram — та же память и задачи.</div>';
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
    del.title = 'Удалить';
    del.onclick = async (e) => {
      e.stopPropagation();
      if (!confirm(`Удалить «${c.title}»?`)) return;
      await api(`/api/conversations/${c.id}`, { method: 'DELETE' });
      if (currentConv === c.id) { currentConv = null; showEmptyState(); $('conv-title').textContent = 'EDITH'; }
      loadConversations();
    };

    item.append(title, del);
    list.appendChild(item);
  });
}

async function openConversation(id, title) {
  currentConv = id;
  $('conv-title').textContent = title || 'EDITH';
  closeSidebar();
  const res = await api(`/api/conversations/${id}/messages`);
  const { messages } = await res.json();
  $('messages').innerHTML = '';
  if (!messages.length) showEmptyState();
  messages.forEach((m) => addMessage(m.role, m.content));
  loadConversations();
}

function newConversation() {
  currentConv = null;
  $('conv-title').textContent = 'EDITH';
  showEmptyState();
  closeSidebar();
  $('input').focus();
}

function openSidebar() { $('sidebar').classList.add('open'); $('scrim').classList.remove('hidden'); }
function closeSidebar() { $('sidebar').classList.remove('open'); $('scrim').classList.add('hidden'); }

// ------------------------------------------------------------------- чат

async function send(text) {
  if (streaming) return;
  streaming = true;
  $('send').disabled = true;

  if (!$('messages').querySelector('.msg')) $('messages').innerHTML = '';
  addMessage('user', text);

  const body = addMessage('assistant', '');
  body.innerHTML = '<div class="typing"><span></span><span></span><span></span></div>';

  let answer = '';
  try {
    const res = await api('/api/chat', {
      method: 'POST',
      body: JSON.stringify({ message: text, conversation_id: currentConv }),
    });

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      // SSE-события разделены пустой строкой.
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
        } else if (evMatch[1] === 'delta') {
          answer += payload.text;
          body.innerHTML = renderMarkdown(answer);
          scrollDown();
        }
      }
    }
  } catch (err) {
    console.error(err);
    if (!answer) body.innerHTML = '<p>Связь с сервером оборвалась. Ответ мог сохраниться — обнови страницу.</p>';
  } finally {
    streaming = false;
    $('send').disabled = false;
    if (!answer && !body.textContent.trim()) {
      body.innerHTML = '<p>Пустой ответ.</p>';
    }
    loadConversations();
  }
}

// ------------------------------------------------------------------- старт

function initApp() {
  $('gate').classList.add('hidden');
  $('app').classList.remove('hidden');
  showEmptyState();
  loadConversations();

  const input = $('input');

  // Автовысота поля: одна строка по умолчанию, растёт под длинный текст.
  input.addEventListener('input', () => {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 180) + 'px';
  });

  // Enter отправляет, Shift+Enter — перенос. На телефоне Enter всегда
  // переносит: там это единственный способ написать абзац, а кнопка
  // отправки под большим пальцем.
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

  $('new-chat').onclick = newConversation;
  $('open-sidebar').onclick = openSidebar;
  $('close-sidebar').onclick = closeSidebar;
  $('scrim').onclick = closeSidebar;
}

async function tryToken(candidate) {
  const res = await fetch('/api/conversations', { headers: { 'X-UI-Token': candidate } });
  return res.ok;
}

(async function boot() {
  if (token && await tryToken(token)) {
    initApp();
    return;
  }
  localStorage.removeItem(TOKEN_KEY);

  const submit = async () => {
    const candidate = $('token-input').value.trim();
    if (!candidate) return;
    if (await tryToken(candidate)) {
      token = candidate;
      localStorage.setItem(TOKEN_KEY, candidate);
      initApp();
    } else {
      $('gate-error').textContent = 'Неверный токен';
    }
  };

  $('token-submit').onclick = submit;
  $('token-input').addEventListener('keydown', (e) => { if (e.key === 'Enter') submit(); });
})();
