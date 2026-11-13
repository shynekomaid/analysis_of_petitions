"""Загальносайтовий аналіз петицій до КМУ: статуси, відповіді, строки, автори, підписанти, імена."""
from __future__ import annotations

import bisect
import collections
import html
from array import array
import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .analysis import KYIV, THRESHOLD, case_kind, gender

TAG_RE = re.compile(r"<[^>]+>")


def parse_dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def days(a: datetime | None, b: datetime | None) -> float | None:
    return (b - a).total_seconds() / 86400 if a and b else None


def plain(s: str | None) -> str:
    return html.unescape(TAG_RE.sub(" ", s or "")).strip()


def tcase(s: str) -> str:
    return "-".join(w[:1].upper() + w[1:].lower() for w in (s or "").strip().split("-"))


def short_name(first: str, last: str, pat: str) -> str:
    """'ОЛЕКСАНДР', 'Антоненко', 'Сергійович' → 'Олександр А.С.' (ім'я + ініціали прізвища й по батькові)."""
    ini = "".join(f"{x.strip()[0].upper()}." for x in (last, pat) if x and x.strip() and x.strip().lower() != "n/a")
    return f"{tcase(first)} {ini}".strip() or "—"


def split_author(a: str) -> dict:
    parts = (a or "").split()
    return {"lastName": parts[0] if parts else "", "firstName": parts[1] if len(parts) > 1 else "",
            "patronymic": " ".join(parts[2:]) if len(parts) > 2 else ""}


def author_of(p: dict) -> tuple[str, dict | None, str | None]:
    """→ (тип: person|org|unknown, {lastName, firstName, patronymic} | None, регіон)."""
    if p.get("organization") or p.get("organizationId"):
        return "org", None, None
    if p.get("creator"):
        c = p["creator"]
        return "person", c, (c.get("region") or {}).get("name")
    if p.get("author"):
        return "person", split_author(p["author"]), None
    return "unknown", None, None


def median(xs):
    return statistics.median(xs) if xs else None


def quantiles(xs, n=4):
    return statistics.quantiles(xs, n=n) if len(xs) >= n else None


SIG_BUCKETS = [(0, 0, "0"), (1, 9, "1–9"), (10, 99, "10–99"), (100, 999, "100–999"),
               (1000, 9999, "1–10 тис."), (10000, THRESHOLD - 1, "10–25 тис."), (THRESHOLD, 10**9, "25 тис.+")]
REPEAT_BUCKETS = [(1, 1, "1"), (2, 4, "2–4"), (5, 9, "5–9"), (10, 49, "10–49"), (50, 10**9, "50+")]
KEEP_NAME_AT = 20
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]


@dataclass
class YearRow:
    year: int
    n: int = 0
    passed: int = 0
    answered: int = 0
    sigs: list = field(default_factory=list)
    authors_F: int = 0
    authors_M: int = 0


@dataclass
class CatRow:
    id: int
    title: str
    n: int = 0
    sig: int = 0
    passed: int = 0
    sF: int = 0       # підписи жінок (нова платформа)
    sN: int = 0       # усі підписи з відомою статтю
    sU: int = 0       # КАПС


@dataclass
class SiteReport:
    generated_at: datetime
    index_at: datetime
    petitions: list
    n: int = 0
    total_sig: int = 0
    status: dict = field(default_factory=dict)
    years: list = field(default_factory=list)
    buckets: list = field(default_factory=list)
    top: list = field(default_factory=list)
    cats: list = field(default_factory=list)
    # відповіді
    passed: list = field(default_factory=list)          # ≥25k або Supported/Answered
    answered: list = field(default_factory=list)
    answer_delays: list = field(default_factory=list)   # дні
    to_25k: list = field(default_factory=list)          # дні (нова платформа)
    answerers: list = field(default_factory=list)
    awaiting: list = field(default_factory=list)
    answered_below: int = 0
    answer_len_median: float | None = None
    # автори
    authors_F: int = 0
    authors_M: int = 0
    authors_org: int = 0
    authors_unknown: int = 0
    authors_unique: int = 0
    prolific: list = field(default_factory=list)
    regions: list = field(default_factory=list)
    passed_by_author_gender: dict = field(default_factory=dict)
    # підписанти (нова платформа)
    sig_petitions: int = 0
    sig_rows: int = 0
    unique_signers: int = 0
    sF: int = 0
    sM: int = 0
    uF: int = 0
    uM: int = 0
    s_upper: int = 0
    u_upper: int = 0
    u_na: int = 0
    names_F: list = field(default_factory=list)
    names_M: list = field(default_factory=list)
    surnames: list = field(default_factory=list)
    hours: list = field(default_factory=list)
    weekdays: list = field(default_factory=list)
    repeat: list = field(default_factory=list)
    repeat_max: int = 0
    top_signers: list = field(default_factory=list)       # (Ім'я І.Б., петицій, стать)
    top1_share: float = 0.0
    top10_share: float = 0.0
    by_year_signers: list = field(default_factory=list)   # (рік, жінки %, КАПС %, підписів)
    women_by_author: dict = field(default_factory=dict)   # стать автора → (F, N)
    missing_signers: int = 0
    # динаміка, модерація, новачки (нова платформа)
    early: list = field(default_factory=list)        # (pid, підписів за 24 год, за 72 год, набрала?, завершена?)
    curves: list = field(default_factory=list)       # (pid, [кумулятивно на день 0..90]) — для тих, що набрали 25 000
    moderation: list = field(default_factory=list)   # (pid, днів від створення до публікації, рік, набрала?, категорія)
    newcomers: list = field(default_factory=list)    # (pid, підписів, з них перших підписів людини на платформі, старт)
    accounts_monthly: list = field(default_factory=list)  # ('2024-05', приріст макс. ID облікового запису)
    # 4. сигнали аномалій: (pid, підписів, частка за найщільніші 10 хв, частка вночі 00–06, частка нових акаунтів,
    #    частка людей, для яких це єдиний підпис)
    anomalies: list = field(default_factory=list)
    # 5. перетин аудиторій тем: {catA: {catB: людей, що підписали обидві}}, людей у темі
    cat_people: dict = field(default_factory=dict)
    cat_pair: dict = field(default_factory=dict)
    cats_per_person: list = field(default_factory=list)   # (кількість тем, людей)
    # 8. сезонність: ('2024-05', підписів, жінок)
    sig_monthly: list = field(default_factory=list)
    # новачки помісячно: ('2024-03', людей з першим підписом у цьому місяці, [(pid, з них через цю петицію), …топ-3])
    newcomers_monthly: list = field(default_factory=list)
    # 1. координовані групи; 2. автори як підписанти; 3. ранній поштовх рекордсменів; 9. «мертві» петиції
    coord: dict = field(default_factory=dict)
    media: list = field(default_factory=list)       # рядки kmu_media.media_report
    authors_sign: dict = field(default_factory=dict)
    boost: dict = field(default_factory=dict)
    dead: dict = field(default_factory=dict)


def build(index: list, signer_iter, index_at: datetime, on_progress=None) -> SiteReport:
    now = datetime.now(KYIV)
    rep = SiteReport(generated_at=now, index_at=index_at, petitions=index)
    rep.n = len(index)
    rep.total_sig = sum(p.get("signaturesNumber") or 0 for p in index)
    rep.status = dict(collections.Counter(p.get("status") for p in index))
    byid = {p["id"]: p for p in index}

    # ---------------------------------------------------------------- петиції
    years: dict[int, YearRow] = {}
    cats: dict[int, CatRow] = {}
    bucket_c = collections.Counter()
    author_gender: dict[int, str] = {}
    authors = collections.Counter()
    author_sig = collections.Counter()
    author_pass = collections.Counter()
    author_short: dict[str, tuple[str, str]] = {}
    regions = collections.Counter()
    pass_by_g = collections.defaultdict(lambda: [0, 0])
    answerers = collections.Counter()
    alens = []
    for p in index:
        sig = p.get("signaturesNumber") or 0
        st = parse_dt(p.get("startedAt")) or parse_dt(p.get("createdAt"))
        y = st.astimezone(KYIV).year if st else 0
        yr = years.setdefault(y, YearRow(y))
        yr.n += 1
        yr.sigs.append(sig)
        is_pass = sig >= THRESHOLD or p.get("status") == "Supported"  # набрала 25 000
        yr.passed += is_pass
        yr.answered += p.get("status") == "Answered"
        for lo, hi, lab in SIG_BUCKETS:
            if lo <= sig <= hi:
                bucket_c[lab] += 1
        cat = p.get("category") or {}
        cr = cats.setdefault(p.get("categoryId") or 0, CatRow(p.get("categoryId") or 0, cat.get("title") or "Без категорії"))
        cr.n += 1; cr.sig += sig; cr.passed += is_pass

        kind, person, region = author_of(p)
        if kind == "person":
            g = gender(person)
            author_gender[p["id"]] = g
            if g == "F":
                rep.authors_F += 1; yr.authors_F += 1
            elif g == "M":
                rep.authors_M += 1; yr.authors_M += 1
            key = " ".join(tcase(person.get(k) or "") for k in ("lastName", "firstName", "patronymic")).strip()
            authors[key] += 1; author_sig[key] += sig; author_pass[key] += is_pass
            author_short.setdefault(key, (short_name(person.get("firstName") or "", person.get("lastName") or "",
                                                     person.get("patronymic") or ""), g))
            if region:
                regions[region] += 1
            pass_by_g[g][0] += 1; pass_by_g[g][1] += is_pass
        elif kind == "org":
            rep.authors_org += 1
        else:
            rep.authors_unknown += 1

        if is_pass:
            rep.passed.append(p)
        if p.get("status") == "Answered":
            rep.answered.append(p)
            if sig < THRESHOLD:
                rep.answered_below += 1
            base = parse_dt(p.get("supportedAt")) or parse_dt(p.get("endedAt"))
            d = days(base, parse_dt(p.get("answeredAt")))
            if d is not None and d >= 0:
                rep.answer_delays.append(d)
            m = p.get("minister")
            if m:
                answerers[(m.get("position") or "").strip() or "—"] += 1
            if p.get("answer"):
                alens.append(len(plain(p["answer"])))
        if p.get("status") == "Supported":
            rep.awaiting.append(p)
        if p.get("supportedAt") and st:
            d = days(st, parse_dt(p["supportedAt"]))
            if d is not None and d >= 0:
                rep.to_25k.append(d)

    rep.years = [years[y] for y in sorted(years) if y]
    rep.buckets = [(lab, bucket_c[lab]) for _, _, lab in SIG_BUCKETS]
    rep.top = sorted(index, key=lambda p: -(p.get("signaturesNumber") or 0))[:10]
    rep.answerers = answerers.most_common(8)
    rep.answer_len_median = median(alens)
    rep.authors_unique = len(authors)
    rep.prolific = [(author_short[a][0], n, author_sig[a], author_pass[a], author_short[a][1])
                    for a, n in authors.most_common(10)]
    rep.regions = regions.most_common(25)
    rep.passed_by_author_gender = {g: tuple(v) for g, v in pass_by_g.items()}

    # ---------------------------------------------------------------- підписанти
    seen: dict[int, int] = {}
    keep: dict[int, tuple[str, str]] = {}
    names_F, names_M, surnames = collections.Counter(), collections.Counter(), collections.Counter()
    hours = [0] * 24
    wd = [0] * 7
    by_year = collections.defaultdict(lambda: [0, 0, 0])  # рік підпису → [N, F, U]
    wba = collections.defaultdict(lambda: [0, 0])
    sig_pids = set()
    day_max: dict[int, int] = {}            # UTC-доба → максимальний ID облікового запису серед підписів того дня
    first_ts: dict[int, int] = {}           # ID підписанта → час його найпершого підпису на платформі
    pet_ids: list[tuple[dict, int, array, array]] = []
    pet_extra: dict[int, tuple[float, float]] = {}   # pid → (частка піку 10 хв, частка вночі)
    surname_h: dict[int, int] = {}                   # ID → хеш прізвища (для «родинних» пар)
    creator_pets: dict[int, list[int]] = collections.defaultdict(list)
    for p_ in index:
        if p_.get("creatorId"):
            creator_pets[p_["creatorId"]].append(p_["id"])
    author_ids = set(creator_pets)
    author_signs: list[tuple[int, int]] = []         # (ID автора-підписанта, pid)
    catmask: dict[int, int] = {}
    by_month = collections.defaultdict(lambda: [0, 0])
    for p, rows in signer_iter:
        sig_pids.add(p["id"])
        st_dt = parse_dt(p.get("startedAt"))
        start = int(st_dt.timestamp()) if st_dt else (min(r[4] for r in rows) if rows else 0)
        tss = sorted(r[4] for r in rows)
        sig_total = p.get("signaturesNumber") or len(rows)
        passed_p = sig_total >= THRESHOLD or p.get("status") == "Supported"
        finished = p.get("status") != "Approved"
        rep.early.append((p["id"], bisect.bisect_right(tss, start + 86400), bisect.bisect_right(tss, start + 3 * 86400),
                          passed_p, finished))
        if passed_p and tss:
            rep.curves.append((p["id"], [bisect.bisect_right(tss, start + d * 86400) for d in range(0, 91)]))
        peak, j = 0, 0
        for i in range(len(tss)):
            while tss[i] - tss[j] > 600:
                j += 1
            if i - j + 1 > peak:
                peak = i - j + 1
        ids = array("q", (r[0] for r in rows))
        tsa = array("q", (r[4] for r in rows))
        cbit = 1 << (p.get("categoryId") or 0)
        for sid, ts in zip(ids, tsa):
            dd = ts // 86400
            if sid > day_max.get(dd, 0):
                day_max[dd] = sid
            f0 = first_ts.get(sid)
            if f0 is None or ts < f0:
                first_ts[sid] = ts
        pet_ids.append((p, start, ids, tsa))
        rep.sig_petitions += 1
        cr = cats.get(p.get("categoryId") or 0)
        ag = author_gender.get(p["id"], "U")
        pF = pU = pN = 0
        for sid, first, last, pat, ts in rows:
            catmask[sid] = catmask.get(sid, 0) | cbit
            sg = {"firstName": first, "lastName": last, "patronymic": pat}
            g = gender(sg)
            up = case_kind(sg) == "upper"
            rep.sig_rows += 1
            pF += g == "F"; pU += up
            if g == "F":
                rep.sF += 1
            elif g == "M":
                rep.sM += 1
            rep.s_upper += up
            dt = datetime.fromtimestamp(ts, KYIV)
            hours[dt.hour] += 1
            pN += dt.hour < 6
            wd[dt.weekday()] += 1
            bm = by_month[(dt.year, dt.month)]; bm[0] += 1; bm[1] += g == "F"
            yy = by_year[dt.year]; yy[0] += 1; yy[1] += g == "F"; yy[2] += up
            c = seen.get(sid)
            if sid in author_ids:
                author_signs.append((sid, p["id"]))
            if c is None:
                seen[sid] = 1
                surname_h[sid] = hash(last.strip().lower())
                if g == "F":
                    rep.uF += 1; names_F[tcase(first)] += 1
                elif g == "M":
                    rep.uM += 1; names_M[tcase(first)] += 1
                rep.u_upper += up
                rep.u_na += pat.strip().lower() == "n/a"
                if last:
                    surnames[tcase(last)] += 1
            else:
                seen[sid] = c + 1
                if c + 1 == KEEP_NAME_AT:  # ім'я зберігаємо лише для дуже активних — пам'ять
                    keep[sid] = (short_name(first, last, pat), g)
        if cr:
            cr.sF += pF; cr.sN += len(rows); cr.sU += pU
        wba[ag][0] += pF; wba[ag][1] += len(rows)
        if rows:
            pet_extra[p["id"]] = (peak / len(rows), pN / len(rows))
        if on_progress:
            on_progress(rep.sig_petitions)
    rep.unique_signers = len(seen)

    days_sorted = sorted(day_max)
    pref, run = [], 0
    for dd in days_sorted:
        run = max(run, day_max[dd]); pref.append(run)

    def max_id_before(day: int) -> int | None:
        i = bisect.bisect_left(days_sorted, day) - 1
        return pref[i] if i >= 0 else None

    # --- новачки (перший підпис людини) і сигнали аномалій
    month_of = lambda ts: datetime.fromtimestamp(ts, KYIV).strftime("%Y-%m")
    nm_total = collections.Counter(); nm_pet = collections.defaultdict(collections.Counter)
    for p, start, ids, tsa in pet_ids:
        if not ids:
            continue
        entries = 0
        for sid, ts in zip(ids, tsa):
            if first_ts[sid] == ts:
                entries += 1
                m_ = month_of(ts)
                nm_total[m_] += 1; nm_pet[m_][p["id"]] += 1
        rep.newcomers.append((p["id"], len(ids), entries, start))
        thr = max_id_before(start // 86400)
        newacc = sum(1 for x in ids if x > thr) / len(ids) if thr else None
        once = sum(1 for x in ids if seen[x] == 1) / len(ids)
        pk, nt = pet_extra.get(p["id"], (0, 0))
        rep.anomalies.append((p["id"], len(ids), pk, nt, newacc, once))
    rep.coord = _coordination(pet_ids, seen, surname_h)
    rep.boost = _boost(pet_ids, seen)
    rep.authors_sign, rep.dead = _authors_and_dead(index, pet_ids, creator_pets, author_signs, seen)
    del pet_ids, first_ts, surname_h
    rep.newcomers_monthly = [(m_, nm_total[m_], nm_pet[m_].most_common(3)) for m_ in sorted(nm_total)]

    # --- перетин аудиторій тем
    people = collections.Counter(); pair = collections.Counter(); ncat = collections.Counter()
    for m in catmask.values():
        bits = [b for b in range(64) if m >> b & 1]
        ncat[len(bits)] += 1
        for a_ in bits:
            people[a_] += 1
            for b_ in bits:
                if b_ != a_:
                    pair[(a_, b_)] += 1
    rep.cat_people = dict(people)
    rep.cat_pair = {f"{a_}:{b_}": v for (a_, b_), v in pair.items()}
    rep.cats_per_person = sorted(ncat.items())
    rep.sig_monthly = [(f"{y}-{m:02d}", v[0], v[1]) for (y, m), v in sorted(by_month.items())]
    # приріст облікових записів помісячно (за максимальним ID)
    month_max: dict[str, int] = {}
    for dd, mx in zip(days_sorted, pref):
        m = datetime.fromtimestamp(dd * 86400, timezone.utc).strftime("%Y-%m")
        month_max[m] = mx
    ms = sorted(month_max)
    rep.accounts_monthly = [(m, month_max[m] - month_max[ms[i - 1]]) for i, m in enumerate(ms) if i > 0]

    # --- модерація: від створення до публікації (нова платформа)
    for p in index:
        if not p.get("creatorId"):
            continue
        c, st = parse_dt(p.get("createdAt")), parse_dt(p.get("startedAt"))
        d = days(c, st)
        if d is None or d < 0:
            continue
        sig = p.get("signaturesNumber") or 0
        rep.moderation.append((p["id"], d, st.astimezone(KYIV).year, sig >= THRESHOLD or p.get("status") == "Supported",
                               p.get("categoryId") or 0))
    rep.names_F, rep.names_M = names_F.most_common(12), names_M.most_common(12)
    rep.surnames = surnames.most_common(12)
    rep.hours, rep.weekdays = hours, wd
    counts = sorted(seen.values(), reverse=True)
    rc = collections.Counter()
    for c in counts:
        for lo, hi, lab in REPEAT_BUCKETS:
            if lo <= c <= hi:
                rc[lab] += 1
    rep.repeat = [(lab, rc[lab]) for _, _, lab in REPEAT_BUCKETS]
    rep.repeat_max = counts[0] if counts else 0
    rep.top_signers = [(keep[sid][0], c, keep[sid][1], bin(catmask.get(sid, 0)).count("1")) for sid, c in
                       sorted(((sid, c) for sid, c in seen.items() if sid in keep), key=lambda x: -x[1])[:10]]
    tot = sum(counts) or 1
    rep.top1_share = 100 * sum(counts[:max(1, len(counts) // 100)]) / tot if counts else 0
    rep.top10_share = 100 * sum(counts[:max(1, len(counts) // 10)]) / tot if counts else 0
    rep.by_year_signers = [(y, 100 * v[1] / v[0], 100 * v[2] / v[0], v[0]) for y, v in sorted(by_year.items()) if v[0]]
    rep.women_by_author = {g: tuple(v) for g, v in wba.items()}
    expected = [p for p in index if p.get("creatorId") and (p.get("signaturesNumber") or 0) > 0]
    rep.missing_signers = sum(1 for p in expected if p["id"] not in sig_pids)
    rep.cats = sorted(cats.values(), key=lambda c: -c.n)
    return rep


# ------------------------------------------------------------------ 1. координовані групи
COORD_WINDOW = 300      # секунд між підписами
COORD_NEIGH = 10        # скільки наступних підписів дивимося
COORD_MIN_SEEN = 5      # лише люди з 5+ підписами
COORD_MIN_CO = 5        # разом у 5+ петиціях
COORD_MIN_SHARE = 0.5   # і це ≥50% петицій менш активного з пари


def _coordination(pet_ids, seen, surname_h) -> dict:
    pair: dict[int, int] = {}
    last_pid: dict[int, int] = {}   # щоб пара рахувалась раз на петицію
    for p, start, ids, tsa in pet_ids:
        ev = sorted((ts, sid) for sid, ts in zip(ids, tsa) if seen.get(sid, 0) >= COORD_MIN_SEEN)
        n = len(ev)
        for i in range(n):
            ti, a = ev[i]
            for j in range(i + 1, min(n, i + 1 + COORD_NEIGH)):
                tj, b = ev[j]
                if tj - ti > COORD_WINDOW:
                    break
                if a == b:
                    continue
                k = (a << 32 | b) if a < b else (b << 32 | a)
                if last_pid.get(k) != p["id"]:
                    last_pid[k] = p["id"]
                    pair[k] = pair.get(k, 0) + 1
    del last_pid
    strong = []
    for k, co in pair.items():
        if co < COORD_MIN_CO:
            continue
        a, b = k >> 32, k & 0xFFFFFFFF
        if co / min(seen[a], seen[b]) >= COORD_MIN_SHARE:
            strong.append((a, b, co))
    del pair
    # компоненти зв'язності
    parent: dict[int, int] = {}
    def find(x):
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x]); x = parent[x]
        return x
    for a, b, _ in strong:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    groups: dict[int, set] = collections.defaultdict(set)
    for a, b, _ in strong:
        groups[find(a)].add(a); groups[find(b)].add(b)
    member = {m: g for g, ms in groups.items() for m in ms}
    # петиції, де разом підписали 2+ учасники групи
    g_pets: dict[int, collections.Counter] = collections.defaultdict(collections.Counter)
    for p, start, ids, tsa in pet_ids:
        c = collections.Counter(member[s_] for s_ in ids if s_ in member)
        for g, k in c.items():
            if k >= 2:
                g_pets[g][p["id"]] = k
    same = sum(1 for a, b, _ in strong if surname_h.get(a) == surname_h.get(b))
    glist = sorted(((len(ms), sum(seen[m] for m in ms), g_pets[g]) for g, ms in groups.items()), key=lambda x: -x[0])
    return {
        "pairs": len(strong), "pairs_same_surname": same, "groups": len(groups),
        "accounts": len(member), "sizes": collections.Counter(min(sz, 10) for sz, _, _ in glist),
        "top": [(sz, sig, len(gp), gp.most_common(3)) for sz, sig, gp in glist[:10]],
        "petitions": len(set().union(*[set(gp) for _, _, gp in glist])) if glist else 0,
        "window": COORD_WINDOW, "min_co": COORD_MIN_CO, "min_share": COORD_MIN_SHARE, "min_seen": COORD_MIN_SEEN,
    }


# ------------------------------------------------------------------ 3. ранній поштовх рекордсменів
def _boost(pet_ids, seen) -> dict:
    rows = []
    lag_h, lag_r = array("l"), array("l")
    tot = tot10 = tot50 = 0
    for p, start, ids, tsa in pet_ids:
        fd = fd10 = fd50 = 0
        for sid, ts in zip(ids, tsa):
            c = seen[sid]
            if c >= 10:
                tot10 += 1
            if c >= 50:
                tot50 += 1
                lag_h.append((ts - start) // 60)
            elif len(lag_r) < 400000:
                lag_r.append((ts - start) // 60)
            if ts < start + 86400:
                fd += 1; fd10 += c >= 10; fd50 += c >= 50
        tot += len(ids)
        rows.append((p["id"], len(ids), fd, fd10, fd50))
    return {"rows": rows, "total": tot, "total10": tot10, "total50": tot50,
            "lag_heavy_min": statistics.median(lag_h) if lag_h else None,
            "lag_regular_min": statistics.median(lag_r) if lag_r else None}


# ------------------------------------------------------------------ 2. автори-підписанти; 9. «мертві» петиції
def _authors_and_dead(index, pet_ids, creator_pets, author_signs, seen):
    owner = {pid: cid for cid, pids in creator_pets.items() for pid in pids}
    own = {(sid, pid) for sid, pid in author_signs if owner.get(pid) == sid}
    edges = collections.Counter()
    for sid, pid in author_signs:
        o = owner.get(pid)
        if o and o != sid:
            edges[(sid, o)] += 1
    signers_authors = {a for a, _ in edges}
    mutual = {(a, b) for (a, b) in edges if (b, a) in edges and a < b}
    parent = {}
    def find(x):
        while parent.get(x, x) != x:
            x = parent[x]
        return x
    for a, b in mutual:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    comp_members = collections.defaultdict(set)
    for ab in mutual:
        for x in ab:
            comp_members[find(x)].add(x)
    comp = collections.Counter({k: len(v) for k, v in comp_members.items()})
    in_mutual = set().union(*comp_members.values()) if comp_members else set()
    largest = max(comp_members.values(), key=len) if comp_members else set()
    def _ok(p_):
        return (p_.get("signaturesNumber") or 0) >= 25000 or p_.get("status") == "Supported"
    grp = {"largest": [0, 0], "mutual": [0, 0], "other": [0, 0]}
    for p_ in index:
        cid = p_.get("creatorId")
        if not cid or p_.get("status") == "Approved":
            continue
        k = "largest" if cid in largest else "mutual" if cid in in_mutual else "other"
        grp[k][0] += 1; grp[k][1] += _ok(p_)
        if k == "largest":
            grp["mutual"][0] += 1; grp["mutual"][1] += _ok(p_)
    with_signers = {p["id"] for p, *_ in pet_ids}
    new_pets = [p for p in index if p.get("creatorId")]
    own_pets = {pid for _, pid in own}
    authors_total = len(creator_pets)
    a_sign = sum(1 for _ in author_signs)

    # «мертві» — завершені петиції нової платформи з <10 підписами
    first = {p["id"]: (min(tsa) - start if len(tsa) else None) for p, start, ids, tsa in pet_ids}
    dead = [p for p in new_pets if (p.get("signaturesNumber") or 0) < 10 and p.get("status") != "Approved"]
    dead_zero = sum(1 for p in dead if (p.get("signaturesNumber") or 0) == 0)
    dead_own = sum(1 for p in dead if p["id"] in own_pets)
    lags = [first[p["id"]] / 3600 for p in dead if first.get(p["id"]) is not None]
    alive = [p for p in new_pets if (p.get("signaturesNumber") or 0) >= 10]
    alive_own = sum(1 for p in alive if p["id"] in own_pets)
    return ({
        "petitions": len(new_pets), "own_signed": len(own_pets & with_signers),
        "authors": authors_total, "authors_signing_others": len(signers_authors),
        "mutual_pairs": len(mutual), "mutual_largest": max(comp.values()) if comp else 0,
        "mutual_groups": len(comp), "author_signatures": a_sign,
        "top_cross": edges.most_common(1)[0][1] if edges else 0,
        "success": {k: tuple(v) for k, v in grp.items()},
        "community_sizes": sorted(comp.values(), reverse=True)[:12],
    }, {
        "n": len(dead), "zero": dead_zero, "own": dead_own, "share": len(dead) / max(1, len([p for p in new_pets if p.get("status") != "Approved"])),
        "lag_median_h": statistics.median(lags) if lags else None, "alive": len(alive), "alive_own": alive_own,
    })
