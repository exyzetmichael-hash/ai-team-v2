#!/usr/bin/env python3
"""
Свод результатов сабагентов: дедуп, ранжирование, нумерация, поиск конфликтов.

    python sources.py findings.json > merged.json
    python sources.py findings.json --volatility fast
    python sources.py findings.json --pretty      # человекочитаемый вид

Вход: JSON-массив ответов сабагентов вида
    [{"subquery": "...", "findings": [{claim, url, title, date, confidence}], "notes": "..."}]

Выход: {"sources": [...], "findings": [...], "conflicts": [...], "stats": {...}}
Номера источников в sources[].n — те самые [n] для цитирования в ответе.

Только стандартная библиотека, зависимостей нет.
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import date, datetime
from urllib.parse import urlparse

# ---------------------------------------------------------------- веса
# Менять безопасно. Обоснование — в references/source-ranking.md.

TIER_4 = [  # первоисточник: госорганы и официальные площадки
    "gov.ru", "nalog.ru", "cbr.ru", "pravo.gov.ru", "consultant.ru",
    "garant.ru", "rosstat.gov.ru", "minobrnauki.gov.ru", "gosuslugi.ru",
]
TIER_3 = [  # академическое и стандарты
    "arxiv.org", "doi.org", "ieee.org", "acm.org", "nature.com",
    "science.org", "springer.com", "rfc-editor.org", "elibrary.ru",
]
TIER_2 = [  # профильные СМИ и качественные тех-площадки
    "reuters.com", "bloomberg.com", "ft.com", "wsj.com", "economist.com",
    "vedomosti.ru", "kommersant.ru", "rbc.ru", "interfax.ru", "tass.ru",
    "habr.com", "vc.ru", "cnews.ru",
]
TIER_1 = [  # обычные СМИ, документация третьих лиц, солидные блоги
    "stackoverflow.com", "github.com", "medium.com", "dev.to",
    "wikipedia.org", "ria.ru", "lenta.ru", "forbes.ru",
]
TIER_0_PENALTY = [  # агрегаторы и контент-фермы — вниз
    "pinterest.", "quora.com", "answers.", "otvet.mail.ru",
    "reddit.com", "twitter.com", "x.com", "facebook.com", "vk.com",
]

# Домены, которые считаем официальной документацией / сайтом субъекта.
# Если домен упомянут в самом запросе — он первоисточник по определению.
DOC_MARKERS = ["docs.", "developer.", "developers.", "api.", "seller.", "support.", "help."]

FRESHNESS = {
    "fast":   [(30, 3), (90, 2), (365, 0), (10**9, -2)],
    "normal": [(180, 2), (730, 1), (10**9, -1)],
    "slow":   [(365, 1), (10**9, 0)],
}

CONFIDENCE = {"high": 2, "medium": 1, "low": 0}
CORROBORATION = {1: 0, 2: 1, 3: 2}  # 4+ → 3

STOPWORDS = set("""
и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по только ее мне
было вот от меня еще нет о из ему теперь когда даже ну вдруг ли если уже или ни быть был him
the a an of to in is are was were for on at by with from this that it as be or and not have has
""".split())


# ---------------------------------------------------------------- утилиты

def domain(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


def base_domain(host: str) -> str:
    """example.co.uk -> example.co.uk, sub.example.com -> example.com (грубо)."""
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    if parts[-2] in ("co", "com", "org", "net", "gov", "ac") and len(parts) >= 3:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def domain_tier(url: str) -> int:
    host = domain(url)
    if not host:
        return 0
    for d in TIER_4:
        if host == d or host.endswith("." + d):
            return 4
    if any(host.startswith(m) for m in DOC_MARKERS):
        return 4
    for d in TIER_3:
        if host == d or host.endswith("." + d):
            return 3
    if host.endswith(".edu") or host.endswith(".ac.uk"):
        return 3
    for d in TIER_2:
        if host == d or host.endswith("." + d):
            return 2
    for d in TIER_1:
        if host == d or host.endswith("." + d):
            return 1
    if any(p in host for p in TIER_0_PENALTY):
        return 0
    return 1


def parse_date(value):
    if not value:
        return None
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y", "%d.%m.%Y", "%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    m = re.search(r"(20\d{2})", s)
    if m:
        return date(int(m.group(1)), 1, 1)
    return None


def freshness_score(published, volatility: str, today: date) -> int:
    if published is None:
        return 0
    age = (today - published).days
    if age < 0:
        age = 0
    for limit, score in FRESHNESS.get(volatility, FRESHNESS["normal"]):
        if age <= limit:
            return score
    return 0


def numbers_in(text: str):
    """Числа из текста, нормализованные: 12 400 -> 12400, 4,70 -> 4.7, 4,0 -> 4

    ВАЖНО про хвостовые нули. Наивное `.rstrip(".0")` тут ломает всё: rstrip
    срезает МНОЖЕСТВО символов, а не суффикс, поэтому "100" превращалось в "1",
    "30 000" в "3", "12 400" в "124". Из-за этого детекция конфликтов —
    самое ценное в пайплайне — молча пропускала расхождения: "порог 100 ₽" и
    "порог 1000 ₽" давали одинаковый {"1"} и противоречием не считались.
    Обрезаем нули только ПОСЛЕ десятичной точки.
    """
    cleaned = re.sub(r"(?<=\d)[  ](?=\d)", "", text)
    found = re.findall(r"\d+(?:[.,]\d+)?", cleaned)
    out = set()
    for f in found:
        value = f.replace(",", ".")
        if "." in value:
            value = value.rstrip("0").rstrip(".")
        out.add(value or "0")
    return out


RU_ENDINGS = (
    "ами", "ями", "ого", "его", "ому", "ему", "ыми", "ими", "ах", "ях",
    "ам", "ям", "ов", "ев", "ий", "ый", "ые", "ие", "ая", "яя", "ой", "ей",
    "ем", "ом", "ую", "юю", "а", "я", "ы", "и", "е", "у", "ю", "о", "ь",
)


def stem(word: str) -> str:
    """Грубый стемминг: отсечь окончание, затем обрезать до 6 символов.

    Нужен из-за русской морфологии. Без него «комиссия» и «комиссии»,
    «одежда» и «одежде» считаются разными словами, и сравнение фактов между
    источниками разваливается: подтверждение не находится, конфликты не ловятся.
    Простой обрезки не хватает — «одежда» и «одежде» ровно по 6 символов."""
    for end in RU_ENDINGS:
        if word.endswith(end) and len(word) - len(end) >= 3:
            word = word[: -len(end)]
            break
    return word[:6]


def keywords(text: str):
    words = re.findall(r"[\w\-]{4,}", text.lower())
    return {stem(w) for w in words if w not in STOPWORDS}


def similar(a: str, b: str) -> float:
    ka, kb = keywords(a), keywords(b)
    if not ka or not kb:
        return 0.0
    return len(ka & kb) / min(len(ka), len(kb))


# ---------------------------------------------------------------- ядро

def load(path):
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    if isinstance(raw, dict):
        raw = [raw]
    out, notes = [], []
    for block in raw:
        if not isinstance(block, dict):
            continue
        sub = block.get("subquery", "")
        if block.get("notes"):
            notes.append({"subquery": sub, "note": block["notes"]})
        for f in block.get("findings") or []:
            if not isinstance(f, dict) or not f.get("claim") or not f.get("url"):
                continue
            out.append({
                "claim": str(f["claim"]).strip(),
                "url": str(f["url"]).strip(),
                "title": (f.get("title") or "").strip(),
                "date": f.get("date"),
                "confidence": (f.get("confidence") or "medium").lower(),
                "verbatim": bool(f.get("verbatim")),
                "subquery": sub,
            })
    return out, notes


def dedup(findings):
    """Схлопывает одинаковые факты с одного URL."""
    seen, result = [], []
    for f in findings:
        dup = False
        for kept in result:
            if kept["url"] == f["url"] and similar(kept["claim"], f["claim"]) > 0.75:
                dup = True
                break
        if not dup:
            result.append(f)
    return result


def corroboration(findings):
    """Для каждого факта — сколько независимых доменов говорят похожее."""
    counts = []
    for i, f in enumerate(findings):
        domains = {base_domain(domain(f["url"]))}
        for j, g in enumerate(findings):
            if i == j:
                continue
            if similar(f["claim"], g["claim"]) > 0.55:
                domains.add(base_domain(domain(g["url"])))
        counts.append(len(domains))
    return counts


def find_conflicts(findings):
    """Похожие по смыслу факты с разными числами — вероятное противоречие."""
    conflicts = []
    for i in range(len(findings)):
        for j in range(i + 1, len(findings)):
            a, b = findings[i], findings[j]
            if base_domain(domain(a["url"])) == base_domain(domain(b["url"])):
                continue
            if similar(a["claim"], b["claim"]) < 0.5:
                continue
            na, nb = numbers_in(a["claim"]), numbers_in(b["claim"])
            if na and nb and not (na & nb):
                conflicts.append({
                    "claim_a": a["claim"], "source_a": a["url"], "date_a": a["date"],
                    "claim_b": b["claim"], "source_b": b["url"], "date_b": b["date"],
                    "hint": "разные числа по одному поводу — проверь даты и методику счёта",
                })
    return conflicts


def build(path, volatility, today):
    findings, notes = load(path)
    if not findings:
        return {
            "sources": [], "findings": [], "conflicts": [], "notes": notes,
            "stats": {"findings": 0, "sources": 0},
            "warning": "Сабагенты не вернули ни одного факта. Не сочиняй ответ — "
                       "смотри раздел «Ничего не нашлось» в SKILL.md.",
        }

    findings = dedup(findings)
    corro = corroboration(findings)

    # вес каждого факта
    for f, c in zip(findings, corro):
        pub = parse_date(f["date"])
        f["_tier"] = domain_tier(f["url"])
        f["_fresh"] = freshness_score(pub, volatility, today)
        f["_corro"] = CORROBORATION.get(c, 3)
        f["_conf"] = CONFIDENCE.get(f["confidence"], 1)
        f["score"] = f["_tier"] + f["_fresh"] + f["_corro"] + f["_conf"]
        f["corroborating_domains"] = c

    # источник = URL; вес источника = лучший из его фактов
    by_url = defaultdict(list)
    for f in findings:
        by_url[f["url"]].append(f)

    sources = []
    for url, items in by_url.items():
        best = max(items, key=lambda x: x["score"])
        sources.append({
            "url": url,
            "domain": domain(url),
            "title": best["title"] or domain(url),
            "date": best["date"],
            "tier": best["_tier"],
            "score": best["score"],
            "claims": len(items),
        })

    sources.sort(key=lambda s: (-s["score"], s["domain"]))
    for n, s in enumerate(sources, 1):
        s["n"] = n
    number_of = {s["url"]: s["n"] for s in sources}

    out_findings = sorted(
        [{
            "n": number_of[f["url"]],
            "claim": f["claim"],
            "url": f["url"],
            "date": f["date"],
            "confidence": f["confidence"],
            "verbatim": f["verbatim"],
            "score": f["score"],
            "corroborating_domains": f["corroborating_domains"],
            "subquery": f["subquery"],
            "breakdown": {
                "tier": f["_tier"], "freshness": f["_fresh"],
                "corroboration": f["_corro"], "confidence": f["_conf"],
            },
        } for f in findings],
        key=lambda x: (-x["score"], x["n"]),
    )

    conflicts = find_conflicts(findings)
    undated = sum(1 for s in sources if not parse_date(s["date"]))
    single = sum(1 for f in out_findings if f["corroborating_domains"] == 1)

    return {
        "sources": sources,
        "findings": out_findings,
        "conflicts": conflicts,
        "notes": notes,
        "stats": {
            "findings": len(out_findings),
            "sources": len(sources),
            "domains": len({base_domain(s["domain"]) for s in sources}),
            "conflicts": len(conflicts),
            "undated_sources": undated,
            "single_source_claims": single,
            "volatility": volatility,
            "generated": today.isoformat(),
        },
    }


def render(data) -> str:
    st = data["stats"]
    lines = [
        f"Фактов: {st['findings']} | источников: {st['sources']} "
        f"| доменов: {st.get('domains', 0)} | режим свежести: {st.get('volatility')}",
        "",
        "ИСТОЧНИКИ (номера для цитирования):",
    ]
    for s in data["sources"]:
        d = s["date"] or "дата не указана"
        lines.append(f"  [{s['n']}] {s['title']} — {s['domain']}, {d}  (вес {s['score']}, тир {s['tier']})")
        lines.append(f"       {s['url']}")
    lines += ["", "ФАКТЫ:"]
    for f in data["findings"]:
        mark = " ⚠ один источник" if f["corroborating_domains"] == 1 else ""
        lines.append(f"  [{f['n']}] {f['claim']}{mark}")
    if data["conflicts"]:
        lines += ["", "⚠ РАСХОЖДЕНИЯ (показать оба варианта, не выбирать молча):"]
        for c in data["conflicts"]:
            lines.append(f"  A: {c['claim_a']}  ({c['source_a']}, {c['date_a']})")
            lines.append(f"  B: {c['claim_b']}  ({c['source_b']}, {c['date_b']})")
            lines.append("")
    if data.get("notes"):
        lines += ["", "ЗАМЕТКИ САБАГЕНТОВ (что не нашлось):"]
        for n in data["notes"]:
            lines.append(f"  · {n['subquery']}: {n['note']}")
    if data.get("warning"):
        lines += ["", "⚠ " + data["warning"]]
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser(description="Свод источников для скилла research")
    p.add_argument("input", help="JSON с ответами сабагентов")
    p.add_argument("--volatility", choices=["fast", "normal", "slow"], default="normal",
                   help="насколько быстро протухает тема (см. source-ranking.md)")
    p.add_argument("--pretty", action="store_true", help="человекочитаемый вывод вместо JSON")
    p.add_argument("--today", help="дата в YYYY-MM-DD для тестов")
    args = p.parse_args()

    today = parse_date(args.today) or date.today()
    try:
        data = build(args.input, args.volatility, today)
    except FileNotFoundError:
        sys.exit(f"Файл не найден: {args.input}")
    except json.JSONDecodeError as e:
        sys.exit(f"Невалидный JSON во входном файле: {e}")

    if args.pretty:
        print(render(data))
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
