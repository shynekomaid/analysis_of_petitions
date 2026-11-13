"""Серійні автори (повтори й дублікати петицій) і регіони авторів на душу населення."""
from __future__ import annotations

import collections
import json
import re
from importlib import resources

from .analysis import THRESHOLD
from .kmu_analysis import author_of, plain, short_name, tcase
from .kmu_text import STOP

WORD = re.compile(r"[а-яіїєґa-z0-9'’]{3,}", re.I)
SIM = 0.5  # поріг схожості заголовків (коефіцієнт Жаккара за словами)
AUTHOR_BUCKETS = [(1, 1, "1 петиція"), (2, 4, "2–4"), (5, 9, "5–9"), (10, 49, "10–49"), (50, 10**6, "50+")]


def passed(p) -> bool:
    return (p.get("signaturesNumber") or 0) >= THRESHOLD or p.get("status") == "Supported"


def words(title: str) -> frozenset:
    return frozenset(w.lower().replace("’", "'") for w in WORD.findall(plain(title))) - STOP


def norm_title(title: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", plain(title).lower())).strip()


def jaccard(a: frozenset, b: frozenset) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def author_key(p) -> tuple[str, str, str] | None:
    kind, person, _ = author_of(p)
    if kind != "person":
        return None
    full = " ".join(tcase(person.get(k) or "") for k in ("lastName", "firstName", "patronymic")).strip()
    return full, short_name(person.get("firstName") or "", person.get("lastName") or "", person.get("patronymic") or ""), \
        "F" if (person.get("patronymic") or "").lower().endswith("на") else "M"


def serial(index: list) -> dict:
    by_author: dict[str, list] = collections.defaultdict(list)
    meta = {}
    for p in index:
        k = author_key(p)
        if k:
            by_author[k[0]].append(p)
            meta[k[0]] = k
    # --- розподіл авторів за кількістю петицій
    buckets = []
    for lo, hi, lab in AUTHOR_BUCKETS:
        aa = [a for a, ps in by_author.items() if lo <= len(ps) <= hi]
        pets = [p for a in aa for p in by_author[a]]
        buckets.append((lab, len(aa), len(pets), sum(passed(p) for p in pets)))
    # --- повтори в межах автора: петиція схожа на одну з попередніх петицій того ж автора
    repeats = 0; rep_passed = 0; rep_after_fail = 0
    per_author = []
    for a, ps in by_author.items():
        if len(ps) < 2:
            continue
        ps = sorted(ps, key=lambda p: p.get("startedAt") or p.get("createdAt") or "")
        ws = [words(p.get("title")) for p in ps]
        r = 0; best_group = collections.Counter()
        for i in range(1, len(ps)):
            prev = [j for j in range(i) if jaccard(ws[i], ws[j]) >= SIM]
            if prev:
                r += 1
                rep_passed += passed(ps[i])
                rep_after_fail += any(not passed(ps[j]) and ps[j].get("status") != "Approved" for j in prev)
                best_group[min(prev)] += 1
        repeats += r
        if r:
            g0, gn = best_group.most_common(1)[0]
            per_author.append((a, len(ps), r, gn + 1, plain(ps[g0].get("title")),
                               sum(p.get("signaturesNumber") or 0 for p in ps), sum(passed(p) for p in ps)))
    per_author.sort(key=lambda x: (-x[2], -x[1]))
    # --- однакові заголовки в різних авторів
    groups = collections.defaultdict(list)
    for p in index:
        t = norm_title(p.get("title"))
        if len(t) >= 15:
            groups[t].append(p)
    clones = []
    for t, ps in groups.items():
        authors = {(author_key(p) or ("?",))[0] for p in ps}
        if len(ps) >= 3 and len(authors) >= 2:
            clones.append((plain(ps[0].get("title")), len(ps), len(authors),
                           sum(p.get("signaturesNumber") or 0 for p in ps), max(p.get("signaturesNumber") or 0 for p in ps),
                           sum(passed(p) for p in ps)))
    clones.sort(key=lambda x: -x[1])
    n_person = sum(len(v) for v in by_author.values())
    return {
        "buckets": buckets, "repeats": repeats, "repeat_passed": rep_passed, "repeat_after_fail": rep_after_fail,
        "petitions": n_person, "authors": len(by_author),
        "top": [(meta[a][1], meta[a][2], n, r, g, t, s, ps) for a, n, r, g, t, s, ps in per_author[:10]],
        "clones": clones[:8], "clone_groups": len(clones), "clone_petitions": sum(c[1] for c in clones),
    }


# ------------------------------------------------------------------ регіони
def population() -> dict:
    return json.loads(resources.files("petistat").joinpath("data/population_2022.json").read_text(encoding="utf-8"))


def region_key(name: str | None) -> str | None:
    if not name:
        return None
    n = name.strip()
    if n in ("місто Київ", "м. Київ", "м.Київ"):
        return "м. Київ"
    return n.replace(" область", "").strip()


def regions_per_capita(index: list) -> tuple[list, dict]:
    """→ ([(регіон, населення, авторів, петицій, авторів на 100 тис., петицій на 100 тис., успішних)], довідка)."""
    pop = population()
    authors = collections.defaultdict(set); pets = collections.Counter(); ok = collections.Counter()
    for p in index:
        c = p.get("creator")
        if not c:
            continue
        rk = region_key((c.get("region") or {}).get("name"))
        if not rk:
            continue
        authors[rk].add(c.get("id")); pets[rk] += 1; ok[rk] += passed(p)
    rows = []
    for rk, n in pop["regions"].items():
        a = len(authors.get(rk, ())); pc_ = pets.get(rk, 0)
        rows.append((rk, n, a, pc_, 1e5 * a / n, 1e5 * pc_ / n, ok.get(rk, 0)))
    rows.sort(key=lambda x: -x[4])
    other = sum(pets[k] for k in pets if k not in pop["regions"])
    return rows, {"source": pop["source"], "note": pop["note"], "other": other}


# ------------------------------------------------------------------ 8. стать авторів за темами
def author_gender_by_category(index: list, min_n: int = 60) -> list:
    """→ [(категорія, авторок, авторів, набрали у авторок, набрали у авторів)], впорядковано за часткою жінок."""
    c = collections.defaultdict(lambda: [0, 0, 0, 0, ""])
    for p in index:
        k = author_key(p)
        if not k:
            continue
        row = c[p.get("categoryId") or 0]
        row[4] = (p.get("category") or {}).get("title") or "Без категорії"
        if k[2] == "F":
            row[0] += 1; row[2] += passed(p)
        else:
            row[1] += 1; row[3] += passed(p)
    rows = [(v[4], v[0], v[1], v[2], v[3]) for v in c.values() if v[0] + v[1] >= min_n]
    rows.sort(key=lambda x: -x[1] / (x[1] + x[2]))
    return rows
