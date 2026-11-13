"""Класифікація підписантів і розрахунок метрик."""
from __future__ import annotations

import collections
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
    KYIV = ZoneInfo("Europe/Kyiv")
except Exception:  # pragma: no cover
    from datetime import timedelta
    KYIV = timezone(timedelta(hours=2))

THRESHOLD = 25_000  # підписів, потрібних петиції до КМУ

OLD_NAMES = frozenset("Галина Ганна Надія Любов Віра Ніна Валентина Лідія Раїса Тамара Зінаїда Ярослава Стефанія "
                      "Василь Іван Петро Степан Михайло Микола Ярослав Мирослав Григорій Федір".split())
YOUNG_NAMES = frozenset("Анастасія Дарина Софія Вікторія Діана Аліна Яна Христина Каріна Валерія "
                        "Дмитро Максим Владислав Артем Денис Назар Євген Ростислав".split())
MALE_A_NAMES = frozenset("микола ілля кузьма фома лука сава мина".split())


# ------------------------------------------------------------------ класифікатори
def gender(sg: dict) -> str:
    """Стать за по батькові (-ович / -івна); запасний варіант — ім'я. 'M' | 'F' | 'U'."""
    p = (sg.get("patronymic") or "").strip().lower()
    if re.search(r"(ич|огли|оглу)$", p):
        return "M"
    if re.search(r"(на|кизи|гизи)$", p):
        return "F"
    f = (sg.get("firstName") or "").strip().lower()
    if not f:
        return "U"
    return "F" if f.endswith(("а", "я")) and f not in MALE_A_NAMES else "M"


def gender_by_surname(last: str | None) -> str | None:
    """Стать лише за прізвищем. None — прізвище однакове для обох статей (-енко, -чук, -ишин…)."""
    l = (last or "").strip().lower().split("-")[0]
    if re.search(r"(ська|цька|зька|ова|ева|єва|іна|їна)$", l):
        return "F"
    if re.search(r"(ський|цький|зький|ов|ев|єв)$", l):
        return "M"
    return None


def case_kind(sg: dict) -> str:
    """'upper' — ПІБ повністю КАПСОМ, 'title' — звичайне написання, 'mixed' — інше."""
    kinds = []
    for k in ("lastName", "firstName", "patronymic"):
        v = sg.get(k) or ""
        if v.strip().lower() == "n/a":
            continue
        letters = [c for c in v if c.isalpha()]
        if letters:
            kinds.append("upper" if all(c.isupper() for c in letters)
                         else "lower" if all(c.islower() for c in letters) else "title")
    if kinds and all(k == "upper" for k in kinds):
        return "upper"
    if kinds and all(k == "title" for k in kinds):
        return "title"
    return "mixed"


def is_na(sg: dict) -> bool:
    return (sg.get("patronymic") or "").strip().lower() == "n/a"


def to_kyiv(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(KYIV)


# ------------------------------------------------------------------ моделі
@dataclass
class PetitionStats:
    meta: dict
    rows: list
    n: int = 0
    F: int = 0
    upper: int = 0

    @property
    def id(self) -> int:
        return self.meta["id"]

    @property
    def M(self) -> int:
        return self.n - self.F


@dataclass
class Baseline:
    petitions: list = field(default_factory=list)
    total: int = 0
    upper: int = 0
    F: int = 0
    sizes: list = field(default_factory=list)
    since: str = ""


@dataclass
class Report:
    author: dict
    focus: PetitionStats
    petitions: list[PetitionStats]
    baseline: Baseline
    generated_at: datetime
    # обчислене
    total: int = 0
    total_F: int = 0
    total_upper: int = 0
    unique: int = 0
    surname: dict = field(default_factory=dict)
    surname_ok: int = 0
    days: list = field(default_factory=list)
    hours: list = field(default_factory=list)
    caps_F: int = 0
    caps_M: int = 0
    quintiles: list = field(default_factory=list)
    age_old: float | None = None
    age_young: float | None = None
    na: int = 0
    second: PetitionStats | None = None
    overlap_second: int = 0
    overlap_any: int = 0
    top_names_F: list = field(default_factory=list)
    top_names_M: list = field(default_factory=list)
    avg_per_day: float = 0.0


def annotate(rows: list) -> list:
    for r in rows:
        sg = r["signatory"]
        r["_g"] = gender(sg)
        r["_c"] = case_kind(sg)
    return rows


def petition_stats(meta: dict, rows: list) -> PetitionStats:
    annotate(rows)
    return PetitionStats(meta, rows, len(rows), sum(r["_g"] == "F" for r in rows),
                         sum(r["_c"] == "upper" for r in rows))


def select_baseline(index: list, exclude_creator: int, k: int, since: str,
                    lo: int = 300, hi: int = 6000) -> list:
    """Еталон: k найбільших петицій інших авторів (lo..hi підписів), що стартували з дати since."""
    c = [p for p in index if p.get("creatorId") != exclude_creator
         and lo <= p.get("signaturesNumber", 0) <= hi and (p.get("startedAt") or "") >= since]
    return sorted(c, key=lambda p: -p["signaturesNumber"])[:k]


def baseline_add(b: Baseline, meta: dict, rows: list) -> None:
    b.petitions.append(meta)
    b.total += len(rows)
    b.sizes.append(len(rows))
    for r in rows:
        sg = r["signatory"]
        b.upper += case_kind(sg) == "upper"
        b.F += gender(sg) == "F"


def build_report(author: dict, focus_id: int, stats: list[PetitionStats], baseline: Baseline) -> Report:
    pets = sorted(stats, key=lambda p: -p.n)
    focus = next(p for p in pets if p.id == focus_id)
    now = datetime.now(KYIV)
    rep = Report(author=author, focus=focus, petitions=pets, baseline=baseline, generated_at=now)
    rep.total = sum(p.n for p in pets)
    rep.total_F = sum(p.F for p in pets)
    rep.total_upper = sum(p.upper for p in pets)
    uniq = {r["signatory"]["id"]: r for p in pets for r in p.rows}
    rep.unique = len(uniq)
    rows = focus.rows

    # стать за прізвищем
    sur = collections.Counter()
    for r in rows:
        g = gender_by_surname(r["signatory"].get("lastName"))
        sur[g or "N"] += 1
        rep.surname_ok += bool(g) and g == r["_g"]
    rep.surname = {"F": sur["F"], "M": sur["M"], "N": sur["N"]}

    # динаміка
    days = collections.Counter(to_kyiv(r["signedAt"]).strftime("%Y-%m-%d") for r in rows)
    rep.days = sorted(days.items())
    hours = [0] * 24
    for r in rows:
        hours[to_kyiv(r["signedAt"]).hour] += 1
    rep.hours = hours
    if rows:
        first = min(to_kyiv(r["signedAt"]) for r in rows)
        end = min(now, to_kyiv(focus.meta["endedAt"])) if focus.meta.get("endedAt") else now
        rep.avg_per_day = len(rows) / max(1e-6, (end - first).total_seconds() / 86400)

    # КАПС
    rep.caps_F = sum(r["_c"] == "upper" and r["_g"] == "F" for r in rows)
    rep.caps_M = sum(r["_c"] == "upper" and r["_g"] == "M" for r in rows)
    if len(rows) >= 25:
        ids = sorted(r["signatory"]["id"] for r in rows)
        bounds = [ids[int(len(ids) * q)] for q in (.2, .4, .6, .8)]
        q = collections.defaultdict(lambda: [0, 0])
        for r in rows:
            k = sum(r["signatory"]["id"] >= b for b in bounds)
            q[k][0] += 1
            q[k][1] += r["_c"] == "upper"
        rep.quintiles = [100 * u / n for n, u in q.values() if n]

    def share(names):
        xs = [r["_c"] for r in uniq.values() if (r["signatory"].get("firstName") or "").strip().title() in names]
        return 100 * sum(c == "upper" for c in xs) / len(xs) if len(xs) >= 20 else None
    rep.age_old, rep.age_young = share(OLD_NAMES), share(YOUNG_NAMES)
    rep.na = sum(is_na(r["signatory"]) for r in uniq.values())

    # перетини аудиторій
    others = [p for p in pets if p.id != focus.id]
    s_focus = {r["signatory"]["id"] for r in rows}
    if others:
        rep.second = others[0]
        rep.overlap_second = len(s_focus & {r["signatory"]["id"] for r in rep.second.rows})
        rep.overlap_any = len(s_focus & {r["signatory"]["id"] for p in others for r in p.rows})

    name = lambda g: collections.Counter((r["signatory"].get("firstName") or "").strip().title()
                                         for r in rows if r["_g"] == g).most_common(6)
    rep.top_names_F, rep.top_names_M = name("F"), name("M")
    return rep
