"""Backend для вкладки Vault в дашборде Hermes — read-only просмотр Obsidian
vault EDITH: дерево заметок, содержимое, граф связей по [[wikilink]].

Работает read-only принципиально: пишет в vault только EDITH (через свой
vault-pr-write скилл, PR-дисциплина с git-историей и ревью). Дашборд —
только смотровое окно, чтобы не заводить второй, неконтролируемый путь
записи в тот же git-репозиторий.

Путь к vault берём в порядке приоритета:
  1. EDITH_VAULT_PATH (если задан в окружении процесса дашборда)
  2. OBSIDIAN_VAULT_PATH из ~/.hermes/profiles/edith/.env (тот же путь,
     что использует сам профиль EDITH — читаем оттуда напрямую, потому что
     .env профиля не обязательно попадает в окружение machine-level
     процесса дашборда)
  3. ~/vault как последний резервный вариант
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query

router = APIRouter()

_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
_SKIP_DIRS = {".git", ".obsidian", ".trash", "node_modules"}


def _read_env_value(env_path: Path, key: str) -> str | None:
    try:
        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() == key:
                return v.strip().strip('"').strip("'")
    except Exception:
        pass
    return None


def _vault_root() -> Path:
    env_override = os.environ.get("EDITH_VAULT_PATH", "").strip()
    if env_override:
        return Path(env_override).expanduser().resolve()

    profile_env = Path.home() / ".hermes" / "profiles" / "edith" / ".env"
    from_profile = _read_env_value(profile_env, "OBSIDIAN_VAULT_PATH")
    if from_profile:
        return Path(from_profile).expanduser().resolve()

    return (Path.home() / "vault").resolve()


def _safe_resolve(rel_path: str) -> Path:
    """Резолвит путь заметки внутри vault, отбивая любой path traversal."""
    root = _vault_root()
    candidate = (root / rel_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=400, detail="Путь вне vault")
    return candidate


def _iter_notes(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            if name.endswith(".md"):
                full = Path(dirpath) / name
                yield full.relative_to(root)


@router.get("/tree")
async def get_tree() -> dict[str, Any]:
    root = _vault_root()
    if not root.exists():
        raise HTTPException(status_code=404, detail=f"Vault не найден: {root}")

    paths = sorted(str(p) for p in _iter_notes(root))
    return {"root": str(root), "notes": paths, "count": len(paths)}


@router.get("/note")
async def get_note(path: str = Query(...)) -> dict[str, Any]:
    full = _safe_resolve(path)
    if not full.is_file() or full.suffix != ".md":
        raise HTTPException(status_code=404, detail="Заметка не найдена")
    content = full.read_text(encoding="utf-8", errors="replace")
    return {"path": path, "content": content}


@router.get("/graph")
async def get_graph() -> dict[str, Any]:
    root = _vault_root()
    if not root.exists():
        raise HTTPException(status_code=404, detail=f"Vault не найден: {root}")

    note_paths = list(_iter_notes(root))
    # Разрешение имён: базовое имя файла (без расширения, в нижнем
    # регистре) -> относительный путь. При дублях имён в разных папках
    # побеждает первый встреченный — известное упрощение v1.
    by_stem: dict[str, str] = {}
    for rel in note_paths:
        stem = rel.stem.lower()
        by_stem.setdefault(stem, str(rel))

    nodes = [{"id": str(rel), "label": rel.stem} for rel in note_paths]
    edges: list[dict[str, str]] = []
    seen_edges: set[tuple[str, str]] = set()

    for rel in note_paths:
        full = root / rel
        try:
            text = full.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        source = str(rel)
        for match in _WIKILINK_RE.finditer(text):
            target_name = match.group(1).strip().lower()
            # Wikilink может указывать на путь с "/" внутри — берём хвост.
            target_name = target_name.rsplit("/", 1)[-1]
            target = by_stem.get(target_name)
            if not target or target == source:
                continue
            edge_key = (source, target)
            if edge_key in seen_edges:
                continue
            seen_edges.add(edge_key)
            edges.append({"source": source, "target": target})

    return {"nodes": nodes, "edges": edges}
