#!/usr/bin/env python3
"""Веб-интерфейс EDITH — свой, вместо Telegram и вместо дашборда Hermes.

ЗАЧЕМ
-----
Михаил про встроенный дашборд Hermes: «нахуй этот дашборд, он мне очень не
нравится, нам нужен свой». Плюс Telegram как единственная дверь к EDITH —
это чужой мессенджер, чужие ограничения форматирования и невозможность
показать рядом с чатом что-то ещё. Свой сервис снимает оба ограничения и
даёт место, куда дальше вешаются фичи «по надобности».

АРХИТЕКТУРА
-----------
    браузер (телефон/комп, оба в tailnet)
      → этот сервис (отдаёт страницу, хранит историю, стримит ответ)
      → gateway EDITH, /v1/chat/completions на 127.0.0.1
        (та же личность, память и инструменты, что в Telegram)

Почему прокси, а не запросы из браузера прямо в gateway: ключ API_SERVER_KEY
не должен оказаться в JS на телефоне — оттуда его вытащит кто угодно, у кого
телефон окажется в руках. Плюс всё, что появится дальше (задачи, деньги,
расписание), считается на сервере, а не в браузере.

Почему история хранится здесь, а не только в Hermes: страницу надо чем-то
наполнять при открытии, и по прошлым разговорам нужен поиск. Отдельная
SQLite рядом с профилем — самый дешёвый способ, ничего не ломающий в Hermes.

Почему только tailnet: сервис даёт полный доступ к EDITH, а через неё — к
почте, задачам, деньгам и календарю Михаила. Публично торчать этому нельзя.
Токен интерфейса — вторая линия, на случай чужого устройства в самой сети.

ЗАПУСК
------
systemd-юнит: ops/hermes-web-ui.service.template
Вручную:  HERMES_HOME=~/.hermes/profiles/edith python3 server.py
"""
from __future__ import annotations

import json
import mimetypes
import os
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator, Optional
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import home_data  # noqa: E402  (после sys.path — иначе не найдётся из systemd)

EDITH_HOME = Path(os.environ.get("HERMES_HOME", "") or (Path.home() / ".hermes" / "profiles" / "edith"))
CONFIG_PATH = EDITH_HOME / "web_ui.json"
DB_PATH = EDITH_HOME / "web_ui.db"
STATIC_DIR = Path(__file__).resolve().parent / "static"

# Сколько последних сообщений диалога уходит в gateway как контекст.
# Не «всю историю»: EDITH и так помнит важное своей памятью, а простыня
# старых сообщений в каждом запросе — это ровно тот расход токенов, ради
# сокращения которого правились настройки компрессии.
DEFAULT_HISTORY_LIMIT = 12

UPSTREAM_TIMEOUT = 300  # EDITH умеет думать подолгу, особенно с сабагентами

_db_lock = threading.Lock()


# --------------------------------------------------------------------------
# конфиг и база
# --------------------------------------------------------------------------

def load_config() -> dict:
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[веб] нет конфига {CONFIG_PATH}: {exc}", file=sys.stderr)
        sys.exit(1)
    for required in ("ui_token", "hermes_api_key"):
        if not cfg.get(required):
            print(f"[веб] в конфиге нет обязательного поля {required!r}", file=sys.stderr)
            sys.exit(1)
    cfg.setdefault("host", "127.0.0.1")
    cfg.setdefault("port", 8700)
    cfg.setdefault("hermes_api_url", "http://127.0.0.1:8642/v1/chat/completions")
    cfg.setdefault("model", "edith")
    cfg.setdefault("history_limit", DEFAULT_HISTORY_LIMIT)
    return cfg


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _db_lock, db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS conversations (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                title       TEXT NOT NULL DEFAULT 'Новый разговор',
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                role            TEXT NOT NULL,
                content         TEXT NOT NULL,
                created_at      TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, id);
        """)


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# --------------------------------------------------------------------------
# операции с диалогами
# --------------------------------------------------------------------------

def create_conversation(title: str = "Новый разговор") -> int:
    with _db_lock, db() as conn:
        cur = conn.execute(
            "INSERT INTO conversations (title, created_at, updated_at) VALUES (?, ?, ?)",
            (title, now(), now()),
        )
        return int(cur.lastrowid)


def list_conversations() -> list[dict]:
    with _db_lock, db() as conn:
        rows = conn.execute(
            """SELECT c.id, c.title, c.updated_at,
                      (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS message_count
               FROM conversations c ORDER BY c.updated_at DESC LIMIT 100"""
        ).fetchall()
    return [dict(r) for r in rows]


def get_messages(conv_id: int, limit: Optional[int] = None) -> list[dict]:
    with _db_lock, db() as conn:
        if limit:
            rows = conn.execute(
                "SELECT role, content, created_at FROM messages WHERE conversation_id = ? "
                "ORDER BY id DESC LIMIT ?", (conv_id, limit),
            ).fetchall()
            rows = list(reversed(rows))
        else:
            rows = conn.execute(
                "SELECT role, content, created_at FROM messages WHERE conversation_id = ? ORDER BY id",
                (conv_id,),
            ).fetchall()
    return [dict(r) for r in rows]


def add_message(conv_id: int, role: str, content: str) -> None:
    with _db_lock, db() as conn:
        conn.execute(
            "INSERT INTO messages (conversation_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (conv_id, role, content, now()),
        )
        conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now(), conv_id))


def maybe_set_title(conv_id: int, first_user_message: str) -> None:
    """Заголовок — первые слова первого сообщения.

    Намеренно БЕЗ вызова модели: у Hermes для этого есть auxiliary-задача
    title_generation, но платить за модель ради подписи в списке — то же
    самое расточительство, от которого мы уходили весь август. Обрезанная
    первая фраза узнаётся не хуже.
    """
    with _db_lock, db() as conn:
        row = conn.execute("SELECT title FROM conversations WHERE id = ?", (conv_id,)).fetchone()
        if not row or row["title"] != "Новый разговор":
            return
        title = " ".join(first_user_message.split())[:60] or "Новый разговор"
        conn.execute("UPDATE conversations SET title = ? WHERE id = ?", (title, conv_id))


# --------------------------------------------------------------------------
# обращение к gateway EDITH
# --------------------------------------------------------------------------

def stream_from_hermes(cfg: dict, messages: list[dict]) -> Iterator[tuple[str, str]]:
    """Шлёт диалог в gateway и отдаёт куски ответа по мере поступления.

    Отдаёт пары («delta», текст) и («status», что делает) — второе, когда
    в потоке видно вызов инструмента.

    Зачем статус: Михаил про долгие паузы — «ощущение, что она тупо хуйнёй
    страдает и тратит мои токены». Строка «смотрю Todoist» снимает ровно
    это: видно, что работа идёт и какая именно.

    Сначала пробуем стриминг: EDITH думает подолгу (память, инструменты,
    сабагенты), и молчащий экран все эти секунды выглядит как поломка.
    Если gateway стриминг не поддерживает — молча падаем на обычный
    ответ целиком, интерфейс этого даже не заметит.
    """
    body = json.dumps({
        "model": cfg["model"],
        "messages": messages,
        "stream": True,
    }).encode("utf-8")

    req = urllib.request.Request(
        cfg["hermes_api_url"],
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg['hermes_api_key']}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT) as resp:
            got_anything = False
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                delta_obj = (chunk.get("choices") or [{}])[0].get("delta", {}) or {}

                # Вызовы инструментов приходят в том же потоке отдельным
                # полем. Точный формат у Hermes живьём не проверен, поэтому
                # разбор защитный: не нашли имя — просто молчим, а не
                # выдумываем, чем она занята.
                for call in (delta_obj.get("tool_calls") or []):
                    name = ((call.get("function") or {}).get("name") or "").strip()
                    if name:
                        yield "status", _tool_label(name)

                delta = delta_obj.get("content")
                if delta:
                    got_anything = True
                    yield "delta", delta
            if got_anything:
                return
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        print(f"[веб] gateway ответил {exc.code} на стриминг: {detail}", file=sys.stderr)
    except Exception as exc:
        print(f"[веб] стриминг не удался ({exc}), пробую обычный запрос", file=sys.stderr)

    yield from _fallback_from_hermes(cfg, messages)


# Человеческие подписи для инструментов. Незнакомый инструмент показываем
# как есть — честнее, чем прятать за общим «работаю».
_TOOL_LABELS = {
    "todoist": "смотрю задачи",
    "calendar": "смотрю календарь",
    "gmail": "смотрю почту",
    "mail": "смотрю почту",
    "sheets": "смотрю таблицу",
    "browser": "открываю страницу",
    "web": "ищу в вебе",
    "search": "ищу",
    "memory": "вспоминаю",
    "delegate": "запустила помощников",
    "terminal": "выполняю команду",
    "skill": "загружаю инструкцию",
}


def _tool_label(name: str) -> str:
    low = name.lower()
    for needle, label in _TOOL_LABELS.items():
        if needle in low:
            return label
    return name


def _fallback_from_hermes(cfg: dict, messages: list[dict]) -> Iterator[tuple[str, str]]:
    body = json.dumps({
        "model": cfg["model"],
        "messages": messages,
        "stream": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        cfg["hermes_api_url"],
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg['hermes_api_key']}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        if content:
            yield "delta", content
        else:
            yield "delta", "EDITH вернула пустой ответ."
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        print(f"[веб] gateway ответил {exc.code}: {detail}", file=sys.stderr)
        yield "delta", f"Не получилось достучаться до EDITH (HTTP {exc.code}). Подробности в journalctl."
    except Exception as exc:
        print(f"[веб] запрос к gateway упал: {exc}", file=sys.stderr)
        yield "delta", "Не получилось достучаться до EDITH. Похоже, gateway не отвечает."


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    config: dict = {}
    protocol_version = "HTTP/1.1"

    # -- вспомогательное ---------------------------------------------------

    def _json(self, code: int, body: Any) -> None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _authorized(self) -> bool:
        return self.headers.get("X-UI-Token", "") == self.config.get("ui_token")

    def _serve_static(self, rel: str) -> None:
        # Никаких «..» — иначе через адресную строку читается весь диск,
        # включая .env и токены Google, лежащие рядом в профиле.
        target = (STATIC_DIR / rel).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
            self._json(404, {"error": "not found"})
            return
        data = target.read_bytes()
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> Optional[dict]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return None

    # -- маршруты ----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path

        if path == "/health":
            self._json(200, {"ok": True})
            return
        if path in ("/", "/index.html"):
            self._serve_static("index.html")
            return
        if path.startswith("/static/"):
            self._serve_static(path[len("/static/"):])
            return

        if path.startswith("/api/"):
            if not self._authorized():
                self._json(401, {"error": "unauthorized"})
                return
            if path == "/api/home":
                self._json(200, home_data.get_home())
                return
            if path == "/api/conversations":
                self._json(200, {"conversations": list_conversations()})
                return
            if path.startswith("/api/conversations/") and path.endswith("/messages"):
                try:
                    conv_id = int(path.split("/")[3])
                except (IndexError, ValueError):
                    self._json(400, {"error": "bad id"})
                    return
                self._json(200, {"messages": get_messages(conv_id)})
                return

        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if not self._authorized():
            self._json(401, {"error": "unauthorized"})
            return

        if path == "/api/conversations":
            self._json(200, {"id": create_conversation()})
            return

        if path == "/api/chat":
            self._handle_chat()
            return

        if path == "/api/stt":
            self._handle_stt()
            return

        self._json(404, {"error": "not found"})

    def _handle_stt(self) -> None:
        """Аудио из браузера → текст. Тело запроса — сырой звук, не JSON."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._json(400, {"error": "bad length"})
            return
        # 25 МБ — потолок Groq. Своя проверка нужна, чтобы не читать в
        # память гигабайт, если что-то пойдёт не так на стороне браузера.
        if length <= 0 or length > 25 * 1024 * 1024:
            self._json(400, {"error": "empty or too large"})
            return

        audio = self.rfile.read(length)
        ext = "webm" if "webm" in (self.headers.get("Content-Type") or "") else "m4a"
        text, error = home_data.transcribe(audio, filename=f"voice.{ext}")
        if error:
            self._json(502, {"error": error})
            return
        self._json(200, {"text": text})

    def do_DELETE(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if not self._authorized():
            self._json(401, {"error": "unauthorized"})
            return
        if path.startswith("/api/conversations/"):
            try:
                conv_id = int(path.rstrip("/").split("/")[-1])
            except ValueError:
                self._json(400, {"error": "bad id"})
                return
            with _db_lock, db() as conn:
                conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conv_id,))
                conn.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
            self._json(200, {"ok": True})
            return
        self._json(404, {"error": "not found"})

    # -- чат ---------------------------------------------------------------

    def _handle_chat(self) -> None:
        payload = self._read_json()
        if not payload:
            self._json(400, {"error": "bad json"})
            return
        text = (payload.get("message") or "").strip()
        if not text:
            self._json(400, {"error": "empty message"})
            return

        conv_id = payload.get("conversation_id")
        if not conv_id:
            conv_id = create_conversation()

        add_message(conv_id, "user", text)
        maybe_set_title(conv_id, text)

        history = get_messages(conv_id, limit=self.config.get("history_limit", DEFAULT_HISTORY_LIMIT))
        upstream_messages = [{"role": m["role"], "content": m["content"]} for m in history]

        # SSE: браузер показывает ответ по мере генерации. Content-Length
        # тут не отдаём — размер заранее неизвестен, поэтому явно просим
        # chunked, иначе HTTP/1.1 соединение зависнет на ожидании тела.
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def send_event(kind: str, data: dict) -> bool:
            """Отправляет одно SSE-событие. False — клиент отвалился."""
            body = f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")
            try:
                self.wfile.write(f"{len(body):X}\r\n".encode("ascii"))
                self.wfile.write(body)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError):
                return False

        send_event("meta", {"conversation_id": conv_id})

        collected: list[str] = []
        client_alive = True
        for kind, piece in stream_from_hermes(self.config, upstream_messages):
            if kind == "status":
                if client_alive:
                    client_alive = send_event("status", {"text": piece})
                continue
            collected.append(piece)
            if client_alive:
                client_alive = send_event("delta", {"text": piece})
            # Даже если клиент закрыл вкладку, дочитываем ответ до конца и
            # сохраняем: EDITH его уже сгенерировала и деньги за него уже
            # потрачены — терять его из-за свёрнутого браузера глупо.

        answer = "".join(collected).strip()
        if answer:
            add_message(conv_id, "assistant", answer)

        if client_alive:
            send_event("done", {"ok": True})
            try:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

    def log_message(self, fmt: str, *args: Any) -> None:
        # Дефолтный лог сыпал бы в journalctl все запросы; содержимое чата
        # сюда не попадает, но и путей достаточно, чтобы это был шум.
        return


def main() -> None:
    cfg = load_config()
    Handler.config = cfg
    init_db()
    # Прогреваем главный экран заранее: первое открытие после рестарта не
    # должно упираться в поход в Todoist и Calendar.
    home_data.warm_cache()
    print(f"[веб] слушаю http://{cfg['host']}:{cfg['port']}", file=sys.stderr)
    ThreadingHTTPServer((cfg["host"], cfg["port"]), Handler).serve_forever()


if __name__ == "__main__":
    main()
