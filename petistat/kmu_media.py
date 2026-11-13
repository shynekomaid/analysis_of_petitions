"""Медіааналіз найбільших петицій: згадки в Google News (публічний RSS, без ключа) проти динаміки підписів.

Для кожної петиції робимо 2 запити «петиція + ключові слова назви», відкидаємо статті поза вікном
[старт − 7 днів; завершення + 60 днів] і ті, заголовок яких не має спільних основ слів із назвою петиції.
Кеш — data/kmu/media/<id>/<ts>.json.gz (знімки, нічого не видаляється).

Обмеження: Google News індексує не всі медіа, пошук за ключовими словами може пропустити статтю
або приписати чужу. Це оцінка медійної уваги, а не повний моніторинг.
"""
from __future__ import annotations

import collections
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from .kmu_analysis import parse_dt, plain
from .kmu_data import KmuStore
from .kmu_text import STOP

RSS = "https://news.google.com/rss/search?"
UA = {"User-Agent": "Mozilla/5.0 (petistat media check)"}
WORD = re.compile(r"[а-яіїєґa-z0-9'’]{4,}", re.I)
ACR = re.compile(r"\b[А-ЯІЇЄҐA-Z]{2,5}\b")   # ВПО, МОЗ, ДСНС, ЗСУ
EXTRA_STOP = {"петиція", "петиції", "петицію", "україни", "україні", "україна", "щодо", "просимо", "вимагаємо"}


def stems(text: str) -> set[str]:
    base = {w.lower()[:5] for w in WORD.findall(text or "")} - {s[:5] for s in STOP | EXTRA_STOP}
    return base | {a.lower() for a in ACR.findall(text or "") if a.lower() not in ("не", "та", "на", "до", "і")}


def keywords(title: str) -> tuple[list[str], list[str]]:
    """→ (слова в порядку назви з абревіатурами на початку, найдовші слова)."""
    t = plain(title)
    mostly_caps = sum(c.isupper() for c in t) > sum(c.isalpha() for c in t) / 2
    acr = [] if mostly_caps else list(dict.fromkeys(a for a in ACR.findall(t) if a.lower() not in ("не", "та", "на", "до")))
    ordered, seen = [], set()
    for w in WORD.findall(t):
        lw = w.lower()
        if lw in STOP | EXTRA_STOP or lw[:5] in seen:
            continue
        seen.add(lw[:5]); ordered.append(lw)
    return acr + ordered, sorted(ordered, key=len, reverse=True)


def build_idf(index: list) -> dict[str, float]:
    """IDF основ слів по заголовках усіх петицій: що рідше слово, то воно інформативніше."""
    import math
    df = collections.Counter()
    for p in index:
        df.update(stems(plain(p.get("title"))))
    n = len(index) or 1
    return {k: math.log(n / v) for k, v in df.items()} | {"__n__": n, "__df__": dict(df)}


def _df(idf: dict, st: str) -> int:
    return idf.get("__df__", {}).get(st, 0)


def _fetch(q: str) -> list[dict]:
    url = RSS + urllib.parse.urlencode({"q": q, "hl": "uk", "gl": "UA", "ceid": "UA:uk"})
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=40) as r:
        root = ET.fromstring(r.read())
    out = []
    for it in root.findall(".//item"):
        try:
            dt = parsedate_to_datetime(it.findtext("pubDate") or "").astimezone(timezone.utc)
        except Exception:
            continue
        title = it.findtext("title") or ""
        src = it.findtext("source") or ""
        if src and title.endswith(" - " + src):
            title = title[: -len(src) - 3]
        out.append({"title": title, "source": src, "link": it.findtext("link") or "", "date": dt.isoformat()})
    return out


def queries(p: dict, idf: dict | None = None) -> list[str]:
    """До трьох запитів по 2 слова з обмеженням дат вікном збору (оператори Google News after:/before:).
    Google об'єднує слова через «І», тож короткі запити дають кращу повноту, а точність забезпечує фільтр."""
    first, longest = keywords(p.get("title"))
    rare = sorted(first, key=lambda w: _df(idf or {}, w.lower()[:5]) or 10**6)
    st = parse_dt(p.get("startedAt")); en = parse_dt(p.get("endedAt")) or st
    if not first or not st:
        return []
    win = f" after:{(st - timedelta(days=7)):%Y-%m-%d} before:{(en + timedelta(days=60)):%Y-%m-%d}"
    qs = [f"петиція {' '.join(rare[:2])}{win}", f"петиція {' '.join(first[:2])}{win}", f"петиція {' '.join(longest[:2])}{win}"]
    return list(dict.fromkeys(qs))


def fetch_media(store: KmuStore, petitions: list, idf: dict, max_age_days: float = 7, progress=None) -> dict[int, list]:
    """→ {pid: [статті]} з кешем-знімками."""
    res = {}
    for i, p in enumerate(petitions, 1):
        d = store.root / "media" / str(p["id"])
        last = store._latest(d)
        qs = queries(p, idf)
        cached = store._read(last[0]) if last else None
        same_q = isinstance(cached, dict) and cached.get("queries") == qs
        fresh = last and (store.offline or (same_q and (datetime.now(timezone.utc) - last[1]).total_seconds() < max_age_days * 86400))
        if fresh:
            res[p["id"]] = cached["items"] if isinstance(cached, dict) else cached
        elif store.offline:
            res[p["id"]] = []
        else:
            items, seen = [], set()
            for q in qs:
                try:
                    for a in _fetch(q):
                        key = a["link"] or a["title"]
                        if key not in seen:
                            seen.add(key); a["query"] = q; items.append(a)
                except Exception:
                    pass
                time.sleep(1.0)  # чемно до Google News
            store._write(d, {"queries": qs, "items": items})
            res[p["id"]] = items
        if progress:
            progress(i)
    return res


RARE_DF = 5       # «унікальне» слово: трапляється не більше ніж у 5 назвах петицій
SCORE_NO_PET = 11.0


def relevant(p: dict, items: list, idf: dict) -> list[dict]:
    """Стаття зараховується, якщо вийшла у вікні збору і:
    • у заголовку є «петиці…» та хоча б одна спільна змістовна основа з назвою петиції, або
    • спільні основи мають сумарний IDF ≥ 11 і серед них є «унікальна» (df ≤ 5)."""
    st = parse_dt(p.get("startedAt")); en = parse_dt(p.get("endedAt")) or st
    if not st:
        return []
    lo, hi = st - timedelta(days=7), en + timedelta(days=60)
    ps = stems(plain(p.get("title")))
    out, seen_t = [], set()
    for a in items:
        dt = datetime.fromisoformat(a["date"])
        if not (lo <= dt <= hi):
            continue
        common = stems(a["title"]) & ps
        if not common:
            continue
        mentions = "петиці" in a["title"].lower()
        score = sum(idf.get(c, 0) for c in common)
        rare = any(0 < _df(idf, c) <= RARE_DF for c in common)
        if not (mentions or (score >= SCORE_NO_PET and rare)):
            continue
        t = a["title"].lower()[:80]
        if t in seen_t:
            continue
        seen_t.add(t); a = dict(a, mentions=mentions); out.append(a)
    return sorted(out, key=lambda a: a["date"])


def media_report(petitions: list, media: dict, signers: dict, idf: dict) -> list[dict]:
    """signers: pid → відсортовані часові мітки підписів (epoch). → рядки для звіту."""
    rows = []
    for p in petitions:
        arts = relevant(p, media.get(p["id"], []), idf)
        st = parse_dt(p.get("startedAt"))
        ts = signers.get(p["id"]) or []
        # фактичний 25 000-й підпис точніший за supportedAt (той фіксується пізніше)
        reached = datetime.fromtimestamp(ts[24999], timezone.utc) if len(ts) >= 25000 else parse_dt(p.get("supportedAt"))
        days = collections.Counter(int((t - st.timestamp()) // 86400) for t in ts) if st else collections.Counter()
        peak_day = max(days, key=days.get) if days else None
        # Google News часто дає лише дату — порівнюємо за календарними днями (день набору 25 000 = «після»)
        rday = reached.date() if reached else None
        before = [a for a in arts if rday is None or datetime.fromisoformat(a["date"]).date() < rday]
        after = [a for a in arts if rday is not None and datetime.fromisoformat(a["date"]).date() >= rday]
        first = datetime.fromisoformat(arts[0]["date"]) if arts else None
        rows.append({
            "p": p, "articles": arts, "before": len(before), "after": len(after),
            "sources": collections.Counter(a["source"] for a in arts),
            "first_day": (first - st).total_seconds() / 86400 if first and st else None,
            "peak_day": peak_day, "peak_n": days.get(peak_day, 0) if peak_day is not None else 0,
            "daily": [days.get(d, 0) for d in range(0, 92)],
            "art_days": [int((datetime.fromisoformat(a["date"]) - st).total_seconds() // 86400) for a in arts] if st else [],
            "reached_day": (reached - st).total_seconds() / 86400 if reached and st else None,
        })
    return rows


def select_targets(rep, top_n: int = 10, magnets_n: int = 8) -> list[dict]:
    """Найбільші петиції нової платформи + «магніти» (з 08.2023), без дублікатів."""
    byid = {p["id"]: p for p in rep.petitions}
    new = [p for p in rep.petitions if p.get("creatorId")]
    top = sorted(new, key=lambda p: -(p.get("signaturesNumber") or 0))[:top_n]
    cutoff = parse_dt("2023-08-01T00:00:00Z").timestamp()
    mag = [byid[x[0]] for x in sorted([x for x in rep.newcomers if x[3] >= cutoff], key=lambda x: -x[2])[:magnets_n]]
    out, seen = [], set()
    for p in mag + top:
        if p["id"] not in seen:
            seen.add(p["id"]); out.append(p)
    return out
