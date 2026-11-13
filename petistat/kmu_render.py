"""Рендер загальносайтового звіту kmustat (A4, 5 сторінок)."""
from __future__ import annotations

import collections
import html
import json
import re
import shutil
import statistics
from importlib import resources
from pathlib import Path

from .analysis import THRESHOLD
from .kmu_analysis import WEEKDAYS, SiteReport, parse_dt, plain, tcase
from .render import DEFAULT_FONT, dec, fmt, font_ctx, load_css, plural, to_pdf

e = html.escape


def pc(a, b, d=1):
    return dec(100 * a / b, d) if b else "—"


def pc_auto(a, b) -> str:
    """Відсоток з точністю, достатньою, щоб мала частка не перетворювалась на «0,0»."""
    if not b:
        return "—"
    v = 100 * a / b
    if v == 0 or v >= 1:
        return dec(v, 1)
    d = 2
    while d < 5 and round(v, d) == 0:
        d += 1
    return dec(v, d)


def dmy(iso):
    return f"{iso[8:10]}.{iso[5:7]}.{iso[:4]}" if iso else "—"


# ------------------------------------------------------------------ SVG
def vbars(items: list[tuple[str, float]], *, W=300, H=120, labels=None, hl=None, cls="m1", fmt_v=None,
          every=1, title="", small=False) -> str:
    """Вертикальні стовпчики. items: (підпис осі, значення)."""
    fmt_v = fmt_v or (lambda v: fmt(int(v)))
    n = len(items) or 1
    mx = max((v for _, v in items), default=1) or 1
    gap = 4 if n < 16 else 2
    bw = (W - gap * (n + 1)) / n
    out = [f'<svg viewBox="0 0 {W} {H+28}" class="chart" role="img" aria-label="{e(title)}">'
           f'<line x1="0" x2="{W}" y1="{H}" y2="{H}" class="axis"/>']
    for i, (lab, v) in enumerate(items):
        x = gap + i * (bw + gap)
        h = max(1.5, (H - 16) * v / mx) if v else 0
        y = H - h
        r = min(3, bw / 2, h / 2) if h else 0
        c = "m2" if hl and hl(i, lab) else cls
        out.append(f'<g><title>{e(lab)}: {fmt_v(v)}</title>')
        if h:
            out.append(f'<path d="M{x:.1f},{H} V{y+r:.1f} q0,-{r:.1f} {r:.1f},-{r:.1f} h{bw-2*r:.1f} '
                       f'q{r:.1f},0 {r:.1f},{r:.1f} V{H} Z" class="{c}"/>')
        if labels is None or labels(i):
            out.append(f'<text x="{x+bw/2:.1f}" y="{y-4:.1f}" class="vl{" sm" if small else ""}">{fmt_v(v)}</text>')
        if i % every == 0 or i == n - 1:
            out.append(f'<text x="{x+bw/2:.1f}" y="{H+13}" class="tl">{e(lab)}</text>')
        out.append("</g>")
    out.append("</svg>")
    return "".join(out)


def hbars(items: list[tuple[str, float, str]], *, mx=None, cls="", label_w="44%", val_w="54px") -> str:
    """Горизонтальні смуги як HTML. items: (підпис, значення, текст значення)."""
    mx = mx or max((v for _, v, _ in items), default=1) or 1
    return "".join(f'<div class="hb {cls}" style="grid-template-columns:{label_w} 1fr {val_w}"><span class="hbl">{e(l)}</span>'
                   f'<div class="hbb"><i style="width:{max(0.5, 100*v/mx):.1f}%"></i></div><b class="num">{t}</b></div>'
                   for l, v, t in items)


def stack2(f: float, la="Жінки", lb="Чоловіки") -> str:
    return (f'<div class="stack"><i class="f" style="width:{f:.2f}%" title="{la} {dec(f)}%"></i>'
            f'<i class="m" style="width:{100-f:.2f}%" title="{lb} {dec(100-f)}%"></i></div>')


def bucketize(xs, edges):
    """edges: [(lo, hi, label)] → [(label, count)]"""
    return [(lab, sum(1 for x in xs if lo <= x <= hi)) for lo, hi, lab in edges]


# ------------------------------------------------------------------ контекст
def short_cat(t: str) -> str:
    t = re.sub(r"\s+", " ", t or "").strip()
    return t if len(t) <= 58 else t[:56].rstrip(" ,") + "…"


def build_context(r: SiteReport) -> dict[str, str]:
    passed, answered = len(r.passed), len(r.answered)
    med = statistics.median([p.get("signaturesNumber") or 0 for p in r.petitions]) if r.petitions else 0
    answered_of_passed = sum(1 for p in r.passed if p.get("status") == "Answered")
    years = r.years
    y_first, y_last = (years[0].year, years[-1].year) if years else (0, 0)

    # --- стор. 1
    # вузькі колонки (1/3 сторінки) — менший viewBox, щоб підписи не дрібнішали
    years_svg = vbars([(str(y.year)[2:] if len(years) > 8 else str(y.year), y.n) for y in years],
                      W=190, H=110, title="Петиції за роками", small=True)
    pass_svg = vbars([(str(y.year)[2:], y.passed) for y in years], cls="m2", title="Набрали 25 000 за роками",
                     W=190, H=110, labels=lambda i: True)
    bk = r.buckets
    buckets = hbars([(lab, n, f"{fmt(n)}") for lab, n in bk], label_w="38%")
    top_rows = "".join(
        f'<tr><td class="num id">{p["id"]}</td><td class="ttl">{e(plain(p.get("title")))}'
        f'<span class="st">{dmy(p.get("startedAt"))} · {e({"Answered": "є відповідь", "Supported": "очікує відповіді", "Unsupported": "не набрала", "Approved": "збір триває"}.get(p.get("status"), p.get("status") or ""))}</span></td>'
        f'<td class="num">{fmt(p.get("signaturesNumber") or 0)}</td></tr>' for p in r.top)

    # --- стор. 2: відповіді
    dl = r.answer_delays
    q = statistics.quantiles(dl, n=4) if len(dl) >= 4 else [0, 0, 0]
    t25 = r.to_25k
    q25 = statistics.quantiles(t25, n=4) if len(t25) >= 4 else [0, 0, 0]
    delay_svg = vbars(bucketize(dl, [(0, 7, "≤7"), (7.0001, 14, "8–14"), (14.0001, 21, "15–21"),
                                     (21.0001, 30, "22–30"), (30.0001, 10**6, "31+")]), title="Днів до відповіді")
    t25_svg = vbars(bucketize(t25, [(0, 7, "≤7"), (7.0001, 30, "8–30"), (30.0001, 60, "31–60"),
                                    (60.0001, 90, "61–90"), (90.0001, 10**6, "90+")]), cls="m2", title="Днів до 25 000")
    cat_pass = sorted([c for c in r.cats if c.n >= 30], key=lambda c: -c.passed / c.n)
    cat_rows = "".join(f'<tr><td class="ttl">{e(short_cat(c.title))}</td><td class="num">{fmt(c.n)}</td>'
                       f'<td class="num">{c.passed}</td><td class="num">{pc(c.passed, c.n)}%</td>'
                       f'<td class="num">{fmt(round(c.sig / c.n))}</td></tr>' for c in cat_pass[:10])
    unanswered = [p for p in r.passed if p.get("status") != "Answered"]
    awaiting_txt = ("Зараз відповіді очікують: " + ", ".join(f"№ {p['id']} ({fmt(p['signaturesNumber'])})" for p in unanswered) + "."
                    if unanswered else "Усі петиції, що набрали 25 000, отримали відповідь.")
    ans_by = ", ".join(f"{e(k)} — {v}" for k, v in r.answerers) or "—"

    # --- стор. 3: автори
    aN = r.authors_F + r.authors_M
    fa = [(str(y.year)[2:], 100 * y.authors_F / (y.authors_F + y.authors_M)) for y in years if y.authors_F + y.authors_M]
    fa_svg = vbars(fa, fmt_v=lambda v: f"{v:.0f}%", cls="f", title="Частка жінок серед авторів")
    pg = r.passed_by_author_gender
    pF = pg.get("F", (0, 0)); pM = pg.get("M", (0, 0))
    dot = lambda g: f'<i class="dot {"f" if g == "F" else "m"}"></i>'
    prolific = "".join(f'<tr><td class="ttl">{dot(g)}{e(a)}</td><td class="num">{n}</td><td class="num">{fmt(s)}</td>'
                       f'<td class="num">{ps}</td></tr>' for a, n, s, ps, g in r.prolific)
    top10_auth = sum(x[1] for x in r.prolific)
    ncats = sum(1 for c in r.cats if c.id)
    top_signers = "".join(f'<tr><td class="num id">{i}</td><td class="ttl">{dot(g)}{e(nm)}</td><td class="num">{c}</td>'
                          f'<td class="num">{k} з {ncats}</td></tr>'
                          for i, (nm, c, g, k) in enumerate(r.top_signers, 1))
    regions = hbars([(reg.replace(" область", " обл."), n, fmt(n)) for reg, n in r.regions[:12]], label_w="42%")
    reg_total = sum(n for _, n in r.regions)

    # --- стор. 4: підписанти
    sN = r.sF + r.sM
    uN = r.uF + r.uM
    cats_w = sorted([c for c in r.cats if c.sN >= 20000], key=lambda c: -c.sF / c.sN)
    cat_women = "".join(
        f'<div class="hb" style="grid-template-columns:46% 1fr 46px"><span class="hbl">{e(short_cat(c.title))}</span>'
        f'{stack2(100 * c.sF / c.sN)}<b class="num">{pc(c.sF, c.sN, 0)}%</b></div>' for c in cats_w)
    wba = r.women_by_author
    wF, wM = wba.get("F", (0, 0)), wba.get("M", (0, 0))
    by_year = "".join(
        f'<tr><td class="num">{y}</td><td class="num">{fmt(n)}</td><td class="num">{dec(f)}%</td><td class="num">{dec(u)}%</td></tr>'
        for y, f, u, n in r.by_year_signers)

    # --- стор. 5: імена та активність
    def names(lst, total):
        return "".join(f'<li><span>{e(n)}</span><b class="num">{fmt(c)}</b><i class="muted num">{pc(c, total)}%</i></li>' for n, c in lst)
    hours_svg = vbars([(f"{h:02d}", v) for h, v in enumerate(r.hours)], labels=lambda i: False, every=3,
                      title="Підписи за годиною доби")
    wd_svg = vbars(list(zip(WEEKDAYS, r.weekdays)), labels=lambda i: False, title="Підписи за днем тижня",
                   hl=lambda i, lab: r.weekdays and r.weekdays[i] == max(r.weekdays))
    rep_svg = hbars([(lab + " " + plural(int(lab.split("–")[0].rstrip("+")), "петиція", "петиції", "петицій"),
                      n, f'{fmt(n)} · {pc_auto(n, r.unique_signers)}%') for lab, n in r.repeat],
                    label_w="32%", val_w="118px")
    peak_h = max(range(24), key=lambda h: r.hours[h]) if any(r.hours) else 0
    one = dict(r.repeat).get("1", 0)

    # --- тексти, що залежать від даних
    pre = [y.passed for y in years if y.year < 2023]
    post = [y.passed for y in years if y.year >= 2023]
    pass_note = (f"До 2023 року — {min(pre)}–{max(pre)} на рік, з 2023-го — {min(post)}–{max(post)} на рік."
                 if pre and post else "")
    ab = r.answered_below
    ans_below_note = (f"Ще {ab} {plural(ab, 'петиція отримала', 'петиції отримали', 'петицій отримали')} відповідь, не набравши порогу."
                      if ab else "")
    aF = 100 * r.authors_F / aN if aN else 0
    sFp = 100 * r.sF / sN if sN else 0
    lead3 = (f"Серед авторів жінок {dec(aF, 0)}%, серед підписів — {dec(sFp, 0)}%. ")
    if pF[0] and pM[0] and pM[1]:
        x = (pF[1] / pF[0]) / (pM[1] / pM[0])
        lead3 += (f"Петиції авторок набирають 25 000 у {dec(x, 1)} раза частіше, ніж петиції авторів." if x > 1.15 else
                  f"Петиції авторів набирають 25 000 у {dec(1 / x, 1)} раза частіше, ніж петиції авторок." if x < 0.87 else
                  "Шанс набрати 25 000 у авторок і авторів приблизно однаковий.")
    prol_pass = sum(x[3] for x in r.prolific)
    prol_med = statistics.median([x[2] / x[1] for x in r.prolific]) if r.prolific else 0
    prol_note = (f"У середньому в них близько {fmt(round(prol_med))} підписів на петицію"
                 + (f", а поріг подолали {prol_pass}." if prol_pass else ", і жодна не набрала 25 000."))
    cw_note = ""
    if len(cats_w) >= 2:
        hi_, lo_ = cats_w[0], cats_w[-1]
        cw_note = (f"Найвища частка жінок — «{e(short_cat(hi_.title))}» ({pc(hi_.sF, hi_.sN, 0)}%), "
                   f"найнижча — «{e(short_cat(lo_.title))}» ({pc(lo_.sF, lo_.sN, 0)}%).")
    if wF[1] and wM[1]:
        d = 100 * wF[0] / wF[1] - 100 * wM[0] / wM[1]
        wba_note = ("Аудиторія тяжіє до статі автора." if d > 3 else "Стать автора майже не впливає на аудиторію."
                    if abs(d) <= 3 else "Петиції авторів-чоловіків жінки підписують навіть активніше.")
    else:
        wba_note = ""
    top_f = r.names_F[0][0] if r.names_F else "?"
    top_m = r.names_M[0][0] if r.names_M else "?"
    def gen_pl(n):  # Тетяна → Тетяни, Олександр → Олександри (простий множинний)
        if n.endswith("я"): return n[:-1] + "ї" if n[-2:-1] in "аеиіоуюяєї'" else n[:-1] + "і"
        if n.endswith("а"): return n[:-1] + ("і" if n[-2:-1] in "гкхжчшщ" else "и")
        if n.endswith("й"): return n[:-1] + "ї"
        return n + "и"
    h5 = f"{gen_pl(top_f)}, {gen_pl(top_m)} і ті, хто підписує все"
    hs = r.hours
    night = sum(hs[0:6]); total_h = sum(hs) or 1
    night_note = f"З 00:00 до 06:00 ставлять {pc(night, total_h, 1)}% підписів."
    wdv = r.weekdays
    we = (wdv[5] + wdv[6]) / 2 if wdv else 0; wk = sum(wdv[:5]) / 5 if wdv else 0
    weekend_note = (f"У вихідні підписують на {dec(100 * (1 - we / wk), 0)}% менше, ніж у будні." if wk and we < wk
                    else "У вихідні підписують не менше, ніж у будні.")

    return dict(
        PASS_NOTE=pass_note, ANS_BELOW_NOTE=ans_below_note, LEAD3=lead3, PROL_NOTE=prol_note, CW_NOTE=cw_note,
        WBA_NOTE=wba_note, H5=e(h5), NIGHT_NOTE=night_note, WEEKEND_NOTE=weekend_note,
        NOW=f"{r.generated_at:%d.%m.%Y, %H:%M}", Y0=str(y_first), Y1=str(y_last),
        N=fmt(r.n), SIG=dec(r.total_sig / 1e6, 2), SIG_FULL=fmt(r.total_sig), MED=fmt(int(med)),
        PASSED=str(passed), PASSED_P=pc(passed, r.n), ANSWERED=str(answered), ANS_OF_PASSED=f"{answered_of_passed} з {passed}",
        ANS_BELOW=str(r.answered_below), UNSUP_P=pc(r.status.get("Unsupported", 0), r.n, 0),
        ACTIVE=fmt(r.status.get("Approved", 0)),
        U100_P=pc(sum(n for lab, n in bk if lab in ("0", "1–9", "10–99")), r.n, 0),
        YEARS_SVG=years_svg, PASS_SVG=pass_svg, BUCKETS=buckets, TOP_ROWS=top_rows,
        D_MED=dec(statistics.median(dl), 0) if dl else "—", D_Q1=dec(q[0], 0), D_Q3=dec(q[2], 0),
        D_MAX=dec(max(dl), 0) if dl else "—", D_N=str(len(dl)), DELAY_SVG=delay_svg,
        T_MED=dec(statistics.median(t25), 0) if t25 else "—", T_Q1=dec(q25[0], 0), T_Q3=dec(q25[2], 0),
        T_MIN=dec(min(t25), 1) if t25 else "—", T_N=str(len(t25)), T25_SVG=t25_svg,
        CAT_ROWS=cat_rows, AWAITING=awaiting_txt, ANS_BY=ans_by,
        ALEN=fmt(int(r.answer_len_median or 0)),
        A_FP=pc(r.authors_F, aN, 0), A_MP=pc(r.authors_M, aN, 0), A_F=fmt(r.authors_F), A_M=fmt(r.authors_M),
        A_ORG=str(r.authors_org), A_UNIQ=fmt(r.authors_unique), A_STACK=stack2(100 * r.authors_F / aN if aN else 0),
        S_STACK=stack2(100 * r.sF / sN if sN else 0), FA_SVG=fa_svg,
        PF_RATE=pc(pF[1], pF[0], 2), PM_RATE=pc(pM[1], pM[0], 2), PF_N=str(pF[1]), PM_N=str(pM[1]),
        PF_X=dec((pF[1] / pF[0]) / (pM[1] / pM[0]), 1) if pF[0] and pM[0] and pM[1] else "—",
        PROLIFIC=prolific, TOP_SIGNERS=top_signers, TOP10_AUTH=str(top10_auth), TOP10_AUTH_P=pc(top10_auth, r.n, 0),
        REGIONS=regions, REG_TOTAL=fmt(reg_total),
        SP=fmt(r.sig_petitions), SROWS=dec(r.sig_rows / 1e6, 2), UNIQ=dec(r.unique_signers / 1e6, 2),
        SFP=pc(r.sF, sN), SMP=pc(r.sM, sN), UFP=pc(r.uF, uN), CAPS_P=pc(r.u_upper, r.unique_signers), NA=fmt(r.u_na),
        CAT_WOMEN=cat_women, WF=pc(wF[0], wF[1], 0), WM=pc(wM[0], wM[1], 0), BY_YEAR=by_year,
        NAMES_F=names(r.names_F, r.uF), NAMES_M=names(r.names_M, r.uM), SURNAMES=names(r.surnames, r.unique_signers),
        HOURS_SVG=hours_svg, WD_SVG=wd_svg, PEAK_H=f"{peak_h:02d}:00", REPEAT=rep_svg,
        ONE_P=pc(one, r.unique_signers, 0), TOP1=dec(r.top1_share, 0), TOP10=dec(r.top10_share, 0),
        RMAX=str(r.repeat_max), AVG_PER=dec(r.sig_rows / r.unique_signers, 2) if r.unique_signers else "—",
        MISSING=(f"Для {r.missing_signers} петицій підписантів у кеші немає — їх не враховано." if r.missing_signers else
                 "Враховано всіх підписантів усіх петицій нової платформи."),
        PB=r.index_at.strftime("%d.%m.%Y"),
    )


def line_band(xs: list[float], mid: list[float], lo: list[float], hi: list[float], *, W=300, H=130, ymax=None,
              ref=None, ref_label="", xticks=(), title="") -> str:
    """Лінія медіани зі смугою між квартилями (одна вісь Y)."""
    ymax = ymax or max(hi + mid + [ref or 0]) or 1
    xmax = max(xs) or 1
    X = lambda x: 4 + (W - 8) * x / xmax
    Y = lambda y: H - (H - 10) * y / ymax
    band = " ".join(f"{X(x):.1f},{Y(v):.1f}" for x, v in zip(xs, hi)) + " " + \
        " ".join(f"{X(x):.1f},{Y(v):.1f}" for x, v in reversed(list(zip(xs, lo))))
    line = " ".join(f"{X(x):.1f},{Y(v):.1f}" for x, v in zip(xs, mid))
    out = [f'<svg viewBox="0 0 {W} {H+22}" class="chart" role="img" aria-label="{e(title)}">',
           f'<line x1="0" x2="{W}" y1="{H}" y2="{H}" class="axis"/>']
    if ref:
        out.append(f'<line x1="0" x2="{W}" y1="{Y(ref):.1f}" y2="{Y(ref):.1f}" class="refl"/>'
                   f'<text x="{W-2}" y="{Y(ref)-4:.1f}" class="tl" text-anchor="end">{e(ref_label)}</text>')
    out.append(f'<polygon points="{band}" class="band"/><polyline points="{line}" class="ln"/>')
    for x in xticks:
        out.append(f'<text x="{X(x):.1f}" y="{H+14}" class="tl">{x}</text>')
    for x, v in zip(xs, mid):
        out.append(f'<circle cx="{X(x):.1f}" cy="{Y(v):.1f}" r="7" class="hit"><title>день {x}: медіана {fmt(int(v))}</title></circle>')
    out.append("</svg>")
    return "".join(out)


def dynamics_context(r: SiteReport) -> dict[str, str]:
    """Сторінки 6–7: перша доба, модерація, новачки."""
    byid = {p["id"]: p for p in r.petitions}
    fin = [x for x in r.early if x[4]]
    edges = [(0, 9, "0–9"), (10, 99, "10–99"), (100, 499, "100–499"), (500, 999, "500–999"),
             (1000, 2999, "1–3 тис."), (3000, 9999, "3–10 тис."), (10000, 10**9, "10 тис.+")]
    rows, bars = [], []
    for lo, hi, lab in edges:
        g = [x for x in fin if lo <= x[1] <= hi]
        k = sum(x[3] for x in g)
        bars.append((lab, 100 * k / len(g) if g else 0))
        rows.append(f'<tr><td>{lab}</td><td class="num">{fmt(len(g))}</td><td class="num">{k}</td>'
                    f'<td class="num">{pc_auto(k, len(g)) if g else "—"}%</td></tr>')
    early_svg = vbars(bars, fmt_v=lambda v: f"{dec(v, 0)}%", cls="m2", title="Шанс набрати 25 000 за підписами першої доби")
    low = [x for x in fin if x[1] < 100]; high = [x for x in fin if x[1] >= 1000]
    passed_24 = [x[1] for x in fin if x[3]]
    late = sum(1 for x in fin if x[3] and x[1] < 10)

    xs = list(range(0, 91, 5))
    cv = [c for _, c in r.curves]
    def q(d, k):
        vals = sorted(c[d] for c in cv)
        if not vals:
            return 0
        return statistics.quantiles(vals, n=4)[k] if len(vals) >= 4 else statistics.median(vals)
    mid = [statistics.median(c[d] for c in cv) if cv else 0 for d in xs]
    curve_svg = line_band(xs, mid, [q(d, 0) for d in xs], [q(d, 2) for d in xs], ref=THRESHOLD, ref_label="25 000",
                          xticks=(0, 15, 30, 45, 60, 75, 90), title="Накопичення підписів у петицій, що набрали 25 000")
    d10 = statistics.median(c[10] for c in cv) if cv else 0

    mod = r.moderation
    mh = [x[1] * 24 for x in mod]
    mq = statistics.quantiles(mh, n=4) if len(mh) >= 4 else [0, 0, 0]
    mod_svg = vbars(bucketize(mh, [(0, 6, "<6 год"), (6.0001, 12, "6–12"), (12.0001, 24, "12–24"),
                                   (24.0001, 48, "1–2 дн."), (48.0001, 72, "2–3 дн."), (72.0001, 10**6, "3+ дн.")]),
                    title="Від створення до публікації")
    mod_years = "".join(f'<tr><td class="num">{y}</td><td class="num">{fmt(len(v))}</td><td class="num">{dec(statistics.median(v), 0)} год</td></tr>'
                        for y in sorted({x[2] for x in mod}) for v in [[x[1] * 24 for x in mod if x[2] == y]])
    mp = [x[1] * 24 for x in mod if x[3]]; mn = [x[1] * 24 for x in mod if not x[3]]

    nc = r.newcomers
    entries_total = sum(x[2] for x in nc); sig_total = sum(x[1] for x in nc)
    cutoff = parse_dt("2023-08-01T00:00:00Z").timestamp()
    mag = sorted([x for x in nc if x[3] >= cutoff], key=lambda x: -x[2])[:8]
    mag_rows = "".join(f'<tr><td class="num id">{pid}</td><td class="ttl">{e(plain(byid[pid].get("title")))}'
                       f'<span class="st">{dmy(byid[pid].get("startedAt"))}</span></td><td class="num">{fmt(n)}</td>'
                       f'<td class="num">{fmt(k)}</td><td class="num">{pc(k, n, 0)}%</td></tr>' for pid, n, k, _ in mag)
    shares_pass = [x[2] / x[1] for x in nc if x[1] >= THRESHOLD and x[3] >= cutoff]
    shares_small = [x[2] / x[1] for x in nc if 100 <= x[1] < 1000 and x[3] >= cutoff]
    nmo = [x for x in r.newcomers_monthly if x[0] >= "2023-08"]          # без запуску платформи
    cur_month = r.generated_at.strftime("%Y-%m")
    peaks = sorted(nmo, key=lambda x: -x[1])[:3]
    peak_m = {m for m, _, _ in peaks}
    mlab = lambda m: f"{m[5:7]}.{m[2:4]}"
    acc_svg = vbars([(m[:4] if m.endswith("-01") else "", v) for m, v, _ in nmo],
                    labels=lambda i: nmo[i][0] in peak_m, W=600, H=90, hl=lambda i, lab: nmo[i][0] in peak_m,
                    fmt_v=lambda v: f"{dec(v / 1000, 0)} тис.", title="Люди, що вперше підписали петицію, за місяць")
    acc_notes = "".join(
        f'<li><b>{mlab(m)}</b> — {fmt(v)} новачків; найбільше привела «{e(plain(byid[top[0][0]].get("title")))[:110]}» '
        f'(№ {top[0][0]}, {fmt(top[0][1])} осіб)</li>' for m, v, top in sorted(peaks) if top)
    full = [v for m, v, _ in nmo if m != cur_month]
    acc_peak = max(nmo, key=lambda x: x[1]) if nmo else ("—", 0, [])

    return dict(
        E_LOW_N=fmt(len(low)), E_LOW_P=pc_auto(sum(x[3] for x in low), len(low)),
        E_HIGH_N=fmt(len(high)), E_HIGH_P=pc(sum(x[3] for x in high), len(high), 0),
        E_MED_PASS=fmt(int(statistics.median(passed_24))) if passed_24 else "—",
        E_MED_FAIL=fmt(int(statistics.median([x[1] for x in fin if not x[3]]))) if fin else "—",
        E_LATE=str(late), E_FIN=fmt(len(fin)), EARLY_SVG=early_svg, EARLY_ROWS="".join(rows),
        CURVE_SVG=curve_svg, D10=fmt(int(d10)), D10_P=pc(d10, THRESHOLD, 0), CURVES_N=str(len(cv)),
        M_MED=dec(statistics.median(mh), 0) if mh else "—", M_Q3=dec(mq[2], 0), M_MAX=dec(max(mh) / 24, 1) if mh else "—",
        M_N=fmt(len(mh)), MOD_SVG=mod_svg, MOD_YEARS=mod_years,
        M_PASS=dec(statistics.median(mp), 0) if mp else "—", M_FAIL=dec(statistics.median(mn), 0) if mn else "—",
        N_ENTRY_P=pc(entries_total, sig_total, 0), N_ENTRIES=fmt(entries_total),
        N_PASS_SHARE=pc(statistics.median(shares_pass), 1, 0) if shares_pass else "—",
        N_SMALL_SHARE=pc(statistics.median(shares_small), 1, 0) if shares_small else "—",
        MAG_ROWS=mag_rows, ACC_SVG=acc_svg, ACC_NOTES=acc_notes,
        ACC_PEAK=f"{acc_peak[0][5:7]}.{acc_peak[0][:4]}" if nmo else "—",
        ACC_PEAK_N=fmt(acc_peak[1]), ACC_MED=fmt(int(statistics.median(full))) if full else "—",
    )


def line_pct(labels: list[str], vals: list[float], *, W=600, H=110, lo=None, hi=None, cls="lnf", title="") -> str:
    """Лінія частки (%) з підписаною шкалою; шкала обрізана до діапазону даних — це підписано на осі."""
    if not vals:
        return ""
    lo = lo if lo is not None else 10 * int(min(vals) // 10)
    hi = hi if hi is not None else 10 * (int(max(vals) // 10) + 1)
    L = 30
    X = lambda i: L + (W - L - 6) * i / max(1, len(vals) - 1)
    Y = lambda v: 6 + (H - 12) * (hi - v) / (hi - lo)
    out = [f'<svg viewBox="0 0 {W} {H+18}" class="chart" role="img" aria-label="{e(title)}">']
    for g in range(lo, hi + 1, 10):
        out.append(f'<line x1="{L}" x2="{W}" y1="{Y(g):.1f}" y2="{Y(g):.1f}" class="grid"/>'
                   f'<text x="{L-4}" y="{Y(g)+3:.1f}" class="tl" text-anchor="end">{g}%</text>')
    pts = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(vals))
    out.append(f'<polyline points="{pts}" class="{cls}"/>')
    for i, (lab, v) in enumerate(zip(labels, vals)):
        out.append(f'<circle cx="{X(i):.1f}" cy="{Y(v):.1f}" r="5" class="hit"><title>{e(lab)}: {dec(v, 1)}%</title></circle>')
        if lab.endswith("-01"):
            out.append(f'<text x="{X(i):.1f}" y="{H+14}" class="tl">{lab[:4]}</text>')
    out.append("</svg>")
    return "".join(out)


LOC = dict(zip(["Січ", "Лют", "Бер", "Кві", "Тра", "Чер", "Лип", "Сер", "Вер", "Жов", "Лис", "Гру"],
               ["у січні", "у лютому", "у березні", "у квітні", "у травні", "у червні", "у липні", "у серпні",
                "у вересні", "у жовтні", "у листопаді", "у грудні"]))
H11 = {"explain": "Найчастіше уряд просто пояснює чинні норми", "promise": "«Опрацьовується» — найчастіша відповідь",
       "support": "Найчастіше уряд підтримує петицію", "refuse": "Найчастіше уряд відмовляє"}


def heat_cell(v: float, vmax: float) -> str:
    a = 0.08 + 0.82 * (v / vmax if vmax else 0)
    ink = "#fff" if a > 0.55 else "var(--ink)"
    return f'<td class="num hm" style="background:rgba(98,80,214,{a:.2f});color:{ink}">{dec(v, 0)}</td>'


def extra_context(r: SiteReport) -> dict[str, str]:
    from .kmu_text import classify_answers, title_features, top_words
    byid = {p["id"]: p for p in r.petitions}

    # ---------- 8. сигнали аномалій
    A = [a for a in r.anomalies if a[1] >= 1000]
    keys = [(2, "peak"), (3, "night"), (4, "newacc"), (5, "once")]
    p95 = {}
    for k, nm in keys:
        v = sorted(x[k] for x in A if x[k] is not None)
        p95[k] = statistics.quantiles(v, n=20)[18] if len(v) >= 20 else float("inf")
    def flags(a):
        return [k for k, _ in keys if a[k] is not None and a[k] >= p95[k]]
    ranked = sorted(A, key=lambda a: (-len(flags(a)), -a[2]))[:12]
    def cell(a, k):
        v = a[k]
        if v is None:
            return '<td class="num">—</td>'
        hot = v >= p95[k]
        return f'<td class="num{" hot" if hot else ""}">{dec(100 * v, 1 if k == 2 else 0)}%</td>'
    an_rows = "".join(f'<tr><td class="num id">{a[0]}</td><td class="ttl">{e(plain(byid[a[0]].get("title")))}</td>'
                      f'<td class="num">{fmt(a[1])}</td>' + "".join(cell(a, k) for k, _ in keys) +
                      f'<td class="num">{len(flags(a))}</td></tr>' for a in ranked)
    med = {k: statistics.median([x[k] for x in A if x[k] is not None]) if A else 0 for k, _ in keys}
    multi = sum(1 for a in A if len(flags(a)) >= 2)

    # ---------- 9. перетин аудиторій тем
    cp = {int(k): v for k, v in r.cat_people.items()}
    pair = {tuple(map(int, k.split(":"))): v for k, v in r.cat_pair.items()}
    titles = {c.id: c.title for c in r.cats}
    topc = [c for c, _ in sorted(cp.items(), key=lambda x: -x[1]) if c][:10]
    hm_max = max((100 * pair.get((a, b), 0) / cp[a] for a in topc for b in topc if a != b), default=1)
    heat = ""
    for i, a in enumerate(topc, 1):
        heat += f'<tr><td class="num id">{i}</td><td class="ttl hmt">{e(short_cat(titles.get(a, "")))}</td><td class="num">{fmt(cp[a])}</td>'
        for b in topc:
            heat += '<td class="num hm diag">—</td>' if a == b else heat_cell(100 * pair.get((a, b), 0) / cp[a], hm_max)
        heat += "</tr>"
    heat_head = "".join(f'<th class="r">{i}</th>' for i in range(1, len(topc) + 1))
    cpp = dict(r.cats_per_person); people = sum(cpp.values()) or 1
    breadth = hbars([(f"{k} {plural(k, 'тема', 'теми', 'тем')}" if k < 6 else "6+ тем",
                      v, f"{pc_auto(v, people)}%") for k, v in
                     [(k, cpp.get(k, 0)) for k in range(1, 6)] + [(6, sum(v for k, v in cpp.items() if k >= 6))]],
                    label_w="26%", val_w="60px")
    one_theme = pc(cpp.get(1, 0), people, 0)
    # найсильніший зв'язок
    best = max(((a, b) for a in topc for b in topc if a != b), key=lambda ab: pair.get(ab, 0) / cp[ab[0]], default=None)

    # ---------- 10. заголовки
    feats = title_features(r.petitions)
    feat_html = ""
    for name, rows in feats:
        feat_html += f'<tr class="grp"><td colspan="4">{e(name)}</td></tr>'
        for k, n, ps in rows:
            feat_html += (f'<tr><td class="ttl" style="padding-left:16px">{e(k)}</td><td class="num">{fmt(n)}</td>'
                          f'<td class="num">{ps}</td><td class="num">{pc_auto(ps, n)}%</td></tr>')
    pw, aw = top_words(r.petitions)
    words_pass = "".join(f'<li><span>{e(w)}</span><b class="num">{c}</b><i class="muted num">×{dec(l, 1)}</i></li>' for w, c, l in pw[:12])
    words_all = "".join(f'<li><span>{e(w)}</span><b class="num">{fmt(c)}</b></li>' for w, c in aw[:12])
    fd = {n: {k: (cnt, ps) for k, cnt, ps in rows} for n, rows in feats}
    def rate(n, k):
        cnt, ps = fd.get(n, {}).get(k, (0, 0)); return pc_auto(ps, cnt)
    excl = fd.get("Знак оклику в заголовку", {}).get("так", (0, 0))

    # ---------- 11. відповіді
    cls = classify_answers(r.petitions)
    tot_ans = sum(len(items) for _, _, items in cls) or 1
    ans_bars = hbars([(nm, len(items), f"{len(items)} · {pc(len(items), tot_ans, 0)}%") for _, nm, items in cls],
                     label_w="44%", val_w="72px")
    quotes = ""
    for code, nm, items in cls:
        for p, q in items[:2]:
            quotes += (f'<div class="quote"><div class="qh"><span class="tag t-{code}">{e(nm)}</span> '
                       f'№ {p["id"]} · {e(plain(p.get("title")))[:90]}</div><div class="qt">«{e(q)}»</div></div>')

    # ---------- 12. сезонність
    pm = collections.Counter()
    top_in_month = {}
    for p in r.petitions:
        st = p.get("startedAt")
        if not st:
            continue
        m = st[:7]
        pm[m] += 1
        if (p.get("signaturesNumber") or 0) > (top_in_month.get(m, {}).get("signaturesNumber") or -1):
            top_in_month[m] = p
    months = sorted(pm)
    pet_svg = vbars([(m[:4] if m.endswith("-01") else "", pm[m]) for m in months], labels=lambda i: False,
                    W=600, H=90, title="Петицій за місяць")
    spikes = sorted(months, key=lambda m: -pm[m])[:5]
    spike_rows = "".join(f'<tr><td class="num">{m[5:7]}.{m[:4]}</td><td class="num">{pm[m]}</td>'
                         f'<td class="ttl">{e(plain(top_in_month[m].get("title")))}</td>'
                         f'<td class="num">{fmt(top_in_month[m].get("signaturesNumber") or 0)}</td></tr>' for m in sorted(spikes))
    sm = [x for x in r.sig_monthly if x[1] >= 5000]
    wsh_svg = line_pct([m for m, _, _ in sm], [100 * f / n for _, n, f in sm], title="Частка жінок серед підписів за місяць")
    wv = [(m, 100 * f / n) for m, n, f in sm]
    wmin = min(wv, key=lambda x: x[1]) if wv else ("—", 0); wmax = max(wv, key=lambda x: x[1]) if wv else ("—", 0)
    by_cal = collections.Counter(); by_cal_n = collections.Counter()
    for m in months:
        by_cal[int(m[5:7])] += pm[m]; by_cal_n[int(m[5:7])] += 1
    cal = [(["Січ", "Лют", "Бер", "Кві", "Тра", "Чер", "Лип", "Сер", "Вер", "Жов", "Лис", "Гру"][k - 1],
            by_cal[k] / by_cal_n[k] if by_cal_n[k] else 0) for k in range(1, 13)]
    cal_svg = vbars(cal, fmt_v=lambda v: dec(v, 0), title="Середня кількість петицій за календарний місяць")

    return dict(
        AN_ROWS=an_rows, AN_N=str(len(A)), AN_MULTI=str(multi),
        P95_PEAK=dec(100 * p95[2], 1), P95_NIGHT=dec(100 * p95[3], 0), P95_NEW=dec(100 * p95[4], 0), P95_ONCE=dec(100 * p95[5], 0),
        MED_PEAK=dec(100 * med[2], 1), MED_NIGHT=dec(100 * med[3], 0), MED_NEW=dec(100 * med[4], 0), MED_ONCE=dec(100 * med[5], 0),
        HEAT=heat, HEAT_HEAD=heat_head, BREADTH=breadth, ONE_THEME=one_theme,
        BEST_PAIR=(f"«{e(short_cat(titles.get(best[0], '')))}» → «{e(short_cat(titles.get(best[1], '')))}»: "
                   f"{pc(pair.get(best, 0), cp[best[0]], 0)}%") if best else "—",
        FEATS=feat_html, WORDS_PASS=words_pass, WORDS_ALL=words_all,
        R_CAPS_Y=rate("Заголовок КАПСОМ", "так"), R_CAPS_N=rate("Заголовок КАПСОМ", "ні"),
        R_EXCL=str(excl[1]), R_EXCL_N=fmt(excl[0]),
        R_SHORT=rate("Довжина тексту, символів", "до 1 тис."), R_LONG=rate("Довжина тексту, символів", "3–6 тис."),
        ANS_BARS=ans_bars, QUOTES=quotes, ANS_TOTAL=str(tot_ans),
        PET_SVG=pet_svg, SPIKE_ROWS=spike_rows, WSH_SVG=wsh_svg, CAL_SVG=cal_svg,
        W_LO=str(10 * int(min(v for _, v in wv) // 10)) if wv else "0",
        CAL_NOTE=(lambda hi_, lo_: f"Найбільше петицій у середньому подають {LOC[hi_[0]]} ({dec(hi_[1], 0)}), "
                  f"найменше — {LOC[lo_[0]]} ({dec(lo_[1], 0)}), тобто в {dec(hi_[1] / lo_[1], 1)} раза менше.")(
                  max(cal, key=lambda x: x[1]), min(cal, key=lambda x: x[1])) if cal and min(x[1] for x in cal) else "",
        H11=H11.get(max(cls, key=lambda c: len(c[2]))[0], "Що відповідає уряд") if cls else "Що відповідає уряд",
        W_MIN=f"{dec(wmin[1], 1)}% ({wmin[0][5:7]}.{wmin[0][:4]})" if wv else "—",
        W_MAX=f"{dec(wmax[1], 1)}% ({wmax[0][5:7]}.{wmax[0][:4]})" if wv else "—",
    )


def authors_context(r: SiteReport) -> dict[str, str]:
    from .kmu_authors import regions_per_capita, serial
    sr = serial(r.petitions)
    dot = lambda g: f'<i class="dot {"f" if g == "F" else "m"}"></i>'
    bk = sr["buckets"]
    b_svg = vbars([(lab, 100 * ok / n if n else 0) for lab, a, n, ok in bk], fmt_v=lambda v: f"{dec(v, 2)}%", cls="m2",
                  title="Шанс набрати 25 000 залежно від кількості петицій автора")
    b_rows = "".join(f'<tr><td>{lab}</td><td class="num">{fmt(a)}</td><td class="num">{fmt(n)}</td><td class="num">{ok}</td>'
                     f'<td class="num">{pc_auto(ok, n)}%</td></tr>' for lab, a, n, ok in bk)
    one = bk[0]; many = [b for b in bk if b[0] in ("10–49", "50+")]
    many_n = sum(b[2] for b in many); many_ok = sum(b[3] for b in many)
    top = "".join(f'<tr><td class="ttl" style="white-space:nowrap">{dot(g)}{e(nm)}</td><td class="num">{n}</td><td class="num">{rp}</td>'
                  f'<td class="num">{gs}</td><td class="ttl">{e(t)}</td></tr>' for nm, g, n, rp, gs, t, _, _ in sr["top"])
    clones = "".join(f'<tr><td class="ttl">{e(t)}</td><td class="num">{n}</td><td class="num">{a}</td>'
                     f'<td class="num">{fmt(sg)}</td><td class="num">{fmt(mx)}</td></tr>' for t, n, a, sg, mx, ok in sr["clones"])

    rows, info = regions_per_capita(r.petitions)
    reg_bars = hbars([(rk, a100, dec(a100, 1)) for rk, _, _, _, a100, _, _ in rows], label_w="34%", val_w="40px")
    reg_rows = "".join(f'<tr><td>{e(rk)}</td><td class="num">{dec(n / 1e6, 2)}</td><td class="num">{a}</td><td class="num">{dec(a100, 1)}</td>'
                       f'<td class="num">{pcnt}</td><td class="num">{dec(p100, 1)}</td><td class="num">{ok}</td></tr>'
                       for rk, n, a, pcnt, a100, p100, ok in rows)
    gap = max(rows, key=lambda x: x[5] / x[4] if x[4] else 0)
    ky = rows[0]
    rest = rows[1:]
    med_rest = statistics.median([x[4] for x in rest]) if rest else 0
    return dict(
        S_BSVG=b_svg, S_BROWS=b_rows, S_ONE_P=pc_auto(one[3], one[2]), S_MANY_P=pc_auto(many_ok, many_n),
        S_REPEATS=str(sr["repeats"]), S_REP_PASSED=str(sr["repeat_passed"]), S_REP_FAIL=str(sr["repeat_after_fail"]),
        S_TOP=top, S_CLONES=clones, S_CLONE_G=str(sr["clone_groups"]), S_CLONE_P=str(sr["clone_petitions"]),
        S_SIM=dec(100 * 0.5, 0),
        R_BARS=reg_bars, R_ROWS=reg_rows, R_SRC=e(info["source"]), R_NOTE=e(info["note"]),
        R_TOP=e(ky[0]), R_TOP_V=dec(ky[4], 1), R_MED=dec(med_rest, 1), R_X=dec(ky[4] / med_rest, 1) if med_rest else "—",
        R_GAP=e(gap[0]), R_GAP_A=dec(gap[4], 1), R_GAP_P=dec(gap[5], 1), R_OTHER=str(info["other"]),
    )


ANSWER_EVIDENCE_ID = 6874  # відповідь КМУ, де прямо пояснено, хто присвоює звання Героя


def wrong_context(r: SiteReport) -> dict[str, str]:
    from .kmu_text import WRONG_TYPES, wrong_address
    w = wrong_address(r.petitions)
    n = len(w); sig = sum(p.get("signaturesNumber") or 0 for p, _, _ in w)
    ok = [p for p, _, _ in w if (p.get("signaturesNumber") or 0) >= THRESHOLD]
    types = collections.Counter(c for _, c, _ in w)
    tnames = {c: nm for c, nm, _ in WRONG_TYPES}
    t_bars = hbars([(tnames[c], v, str(v)) for c, v in types.most_common()], label_w="46%", val_w="40px")
    yc = collections.Counter((p.get("startedAt") or "")[:4] for p, _, _ in w)
    yt = collections.Counter((p.get("startedAt") or "")[:4] for p in r.petitions)
    years = [y for y in sorted(yt) if y]
    y_svg = vbars([(y[2:], yc.get(y, 0)) for y in years], cls="m2", title="Петицій не за адресою за рік")
    peak_y = max(years, key=lambda y: yc.get(y, 0)) if years else "—"
    top = sorted(w, key=lambda x: -(x[0].get("signaturesNumber") or 0))[:10]
    rows = "".join(f'<tr><td class="num id">{p["id"]}</td><td class="ttl">{e(plain(p.get("title")))}'
                   f'<span class="st">{dmy(p.get("startedAt"))} · {e(nm)}</span></td>'
                   f'<td class="num">{fmt(p.get("signaturesNumber") or 0)}</td></tr>' for p, _, nm in top)
    byid = {p["id"]: p for p in r.petitions}
    ev = byid.get(ANSWER_EVIDENCE_ID)
    quote = ""
    if ev and ev.get("answer"):
        t = re.sub(r"\s+", " ", plain(ev["answer"]))
        m = re.search(r"подання про присвоєння звання Герой України вносять Президентові України", t)
        if m:
            frag = t[m.start():m.start() + 330]
            frag = frag.rsplit(" ", 1)[0] + "…"
            quote = (f'<div class="quote"><div class="qh">Відповідь уряду на петицію № {ev["id"]} '
                     f'({fmt(ev.get("signaturesNumber") or 0)} підписів)</div><div class="qt">«…{e(frag)}»</div></div>')
    med = statistics.median([p.get("signaturesNumber") or 0 for p, _, _ in w]) if w else 0
    return dict(
        W_N=str(n), W_P=pc(n, r.n, 1), W_SIG=fmt(sig), W_OK=str(len(ok)), W_MED=fmt(int(med)),
        W_OK_LIST=", ".join(f"№ {p['id']}" for p in ok) or "—",
        W_TBARS=t_bars, W_YSVG=y_svg, W_PEAK_Y=peak_y, W_PEAK_N=str(yc.get(peak_y, 0)),
        W_PEAK_P=pc(yc.get(peak_y, 0), yt.get(peak_y, 0), 1), W_ROWS=rows, W_QUOTE=quote,
        W_YNOTE=(f"{pc(types.get('hero', 0), n, 0)}% таких петицій — прохання присвоїти (часто посмертно) або "
                 f"позбавити звання Героя України. Більшість подано починаючи з {min((y for y in years if yc.get(y, 0) >= 10), default='—')} року.")
                if n else "",
    )


def more_context(r: SiteReport) -> dict[str, str]:
    """Сторінки 16–20: рекордсмени й час публікації, групи, автори-підписанти, тексти, стать авторів за темами."""
    from .kmu_authors import author_gender_by_category
    from .kmu_text import bill_refs, publication_time, repeat_after_answer, title_language
    byid = {p["id"]: p for p in r.petitions}
    T = lambda pid, n=90: e(plain(byid[pid].get("title")))[:n] if pid in byid else str(pid)

    # ---------- 16. рекордсмени як аудиторія; час публікації
    b = r.boost
    rows = [x for x in b.get("rows", []) if x[2] > 0]
    size_b = [(0, 99, "до 100"), (100, 999, "100–999"), (1000, THRESHOLD - 1, "1–25 тис."), (THRESHOLD, 10**9, "25 тис.+")]
    bb = []
    for lo, hi, lab in size_b:
        g = [x for x in rows if lo <= x[1] <= hi]
        fd = sum(x[2] for x in g)
        bb.append((lab, 100 * sum(x[3] for x in g) / fd if fd else 0))
    boost_svg = vbars(bb, fmt_v=lambda v: f"{dec(v, 0)}%", cls="m2", title="Частка активних підписантів серед підписів першої доби")
    small = [x for x in rows if x[1] < 100]
    fd_s = sum(x[2] for x in small) or 1
    early = {pid: fd for pid, fd, *_ in r.early}
    pt = publication_time(r.petitions, early)
    h_svg = vbars([(l, 100 * ok / n if n else 0) for l, n, ok, _ in pt["hours"]], fmt_v=lambda v: f"{dec(v, 1)}%",
                  title="Шанс набрати 25 000 за годиною публікації")
    h_rows = "".join(f'<tr><td>{l}</td><td class="num">{fmt(n)}</td><td class="num">{ok}</td><td class="num">{pc_auto(ok, n)}%</td>'
                     f'<td class="num">{fmt(int(m))}</td></tr>' for l, n, ok, m in pt["hours"])
    w_rows = "".join(f'<tr><td>{WEEKDAYS[i]}</td><td class="num">{fmt(n)}</td><td class="num">{ok}</td><td class="num">{pc_auto(ok, n)}%</td>'
                     f'<td class="num">{fmt(int(m))}</td></tr>' for i, n, ok, m in pt["weekdays"])
    tot_pt = sum(n for _, n, _, _ in pt["hours"]) or 1
    work_h = sum(n for l, n, _, _ in pt["hours"] if l in ("06–09", "09–12", "12–15", "15–18"))
    work_d = sum(n for i, n, _, _ in pt["weekdays"] if i < 5)
    best_h = max(pt["hours"], key=lambda x: x[2] / x[1] if x[1] else 0)
    worst_h = min((x for x in pt["hours"] if x[1] >= 100), key=lambda x: x[2] / x[1] if x[1] else 0, default=best_h)

    # ---------- 17. координовані групи
    c = r.coord or {}
    sizes = c.get("sizes", {})
    size_bars = hbars([(f"{k}{'+' if k == 10 else ''} {plural(k, 'учасник', 'учасники', 'учасників')}", v, str(v))
                       for k, v in sorted(sizes.items())], label_w="34%", val_w="40px")
    g_rows = "".join(
        f'<tr><td class="num">{sz}</td><td class="num">{fmt(sig)}</td><td class="num">{npet}</td>'
        f'<td class="ttl">{"; ".join(f"№ {pid} — {T(pid, 60)} ({k} уч.)" for pid, k in ex)}</td></tr>'
        for sz, sig, npet, ex in c.get("top", [])[:8])
    pairs = c.get("pairs", 0)

    # ---------- 18. автори-підписанти; «мертві» петиції
    a = r.authors_sign or {}
    d = r.dead or {}

    # ---------- 19. мова, законопроєкти, повторні вимоги
    L = title_language(r.petitions)
    lname = {"uk": "Українська", "ru": "Російська", "mix": "Змішана", "en": "Англійська / латиниця"}
    l_rows = "".join(f'<tr><td>{lname.get(k, k)}</td><td class="num">{fmt(n)}</td><td class="num">{ok}</td>'
                     f'<td class="num">{pc_auto(ok, n)}%</td><td class="num">{fmt(int(m))}</td></tr>'
                     for k, (n, ok, m) in sorted(L["by"].items(), key=lambda x: -x[1][0]))
    ru_years = [(y[2:], v.get("ru", 0)) for y, v in L["years"].items() if y[:2] == "20"]
    ru_svg = vbars(ru_years, cls="m2", title="Заголовків російською за рік")
    ru = L["by"].get("ru", (0, 0, 0))
    br = bill_refs(r.petitions)
    b_rows = "".join(f'<tr><td>{lab}</td><td class="num">{fmt(br[k][0])}</td><td class="num">{br[k][1]}</td>'
                     f'<td class="num">{pc_auto(br[k][1], br[k][0])}%</td></tr>'
                     for k, lab in (("title", "Номер акта в заголовку"), ("content", "Номер акта лише в тексті"), ("none", "Без посилань")))
    rep = repeat_after_answer(r.petitions)
    rep_rows = "".join(f'<tr><td class="num id">{a_["id"]}</td><td class="ttl">{e(plain(a_.get("title")))}'
                       f'<span class="st">відповідь {dmy(a_.get("answeredAt"))}</span></td><td class="num">{len(f)}</td>'
                       f'<td class="num">{fmt(sum(p.get("signaturesNumber") or 0 for p in f))}</td></tr>' for a_, f in rep[:6])

    # ---------- 20. стать авторів за темами
    gc = author_gender_by_category(r.petitions)
    g_bars = "".join(
        f'<div class="hb" style="grid-template-columns:46% 1fr 46px"><span class="hbl">{e(short_cat(t))}</span>'
        f'{stack2(100 * f / (f + m))}<b class="num">{pc(f, f + m, 0)}%</b></div>' for t, f, m, _, _ in gc)
    g_rows = g_rows
    gs_rows = "".join(f'<tr><td class="ttl">{e(short_cat(t))}</td><td class="num">{f}</td><td class="num">{fp}</td>'
                      f'<td class="num">{pc_auto(fp, f)}%</td><td class="num">{m}</td><td class="num">{mp}</td><td class="num">{pc_auto(mp, m)}%</td></tr>'
                      for t, f, m, fp, mp in gc)
    return dict(
        B_ALL10=pc(b.get("total10", 0), b.get("total", 1), 1), B_ALL50=pc(b.get("total50", 0), b.get("total", 1), 1),
        B_SMALL10=pc(sum(x[3] for x in small), fd_s, 0), B_SMALL50=pc(sum(x[4] for x in small), fd_s, 0),
        B_LAG_H=dec((b.get("lag_heavy_min") or 0) / 1440, 1), B_LAG_R=dec((b.get("lag_regular_min") or 0) / 1440, 1),
        PT_WORK_H=pc(work_h, tot_pt, 0), PT_WORK_D=pc(work_d, tot_pt, 1),
        AS_SUCC=hbars([(lab, 100 * a["success"][k][1] / a["success"][k][0] if a.get("success", {}).get(k, (0, 0))[0] else 0,
                        f'{pc_auto(a["success"][k][1], a["success"][k][0])}% · {fmt(a["success"][k][0])}')
                       for k, lab in (("largest", "Найбільша спільнота"), ("mutual", "Усі спільноти взаємної підтримки"),
                                      ("other", "Інші автори"))] if a.get("success") else [], label_w="40%", val_w="96px"),
        AS_COMM=", ".join(str(x) for x in a.get("community_sizes", [])),
        B_SVG=boost_svg, PT_SVG=h_svg, PT_HROWS=h_rows, PT_WROWS=w_rows,
        PT_BEST=best_h[0], PT_BEST_P=pc_auto(best_h[2], best_h[1]), PT_WORST=worst_h[0], PT_WORST_P=pc_auto(worst_h[2], worst_h[1]),
        C_GROUPS=fmt(c.get("groups", 0)), C_ACC=fmt(c.get("accounts", 0)), C_PAIRS=fmt(pairs),
        C_SAME=pc(c.get("pairs_same_surname", 0), pairs, 0) if pairs else "0", C_PETS=fmt(c.get("petitions", 0)),
        C_WIN=str(c.get("window", 300) // 60), C_MINCO=str(c.get("min_co", 5)), C_SHARE=dec(100 * c.get("min_share", .5), 0),
        C_MINSEEN=str(c.get("min_seen", 5)), C_SIZES=size_bars, C_ROWS=g_rows,
        AS_OWN=pc(a.get("own_signed", 0), a.get("petitions", 1), 0), AS_OTHERS=pc(a.get("authors_signing_others", 0), a.get("authors", 1), 0),
        AS_MUTUAL=fmt(a.get("mutual_pairs", 0)), AS_LARGEST=fmt(a.get("mutual_largest", 0)), AS_GROUPS=fmt(a.get("mutual_groups", 0)),
        AS_SIGS=fmt(a.get("author_signatures", 0)), AS_SIGS_P=pc(a.get("author_signatures", 0), r.sig_rows or 1, 1),
        D_N=fmt(d.get("n", 0)), D_SHARE=dec(100 * d.get("share", 0), 1), D_ZERO=str(d.get("zero", 0)),
        D_OWN=pc(d.get("own", 0), d.get("n", 1), 0), D_ALIVE_OWN=pc(d.get("alive_own", 0), d.get("alive", 1), 0),
        D_LAG=dec(d.get("lag_median_h") or 0, 1),
        L_ROWS=l_rows, RU_SVG=ru_svg, RU_N=fmt(ru[0]), RU_MED=fmt(int(ru[2])), UK_MED=fmt(int(L["by"].get("uk", (0, 0, 0))[2])),
        BILL_ROWS=b_rows, BILL_X=dec((br["content"][1] / br["content"][0]) / (br["none"][1] / br["none"][0]), 1)
        if br["content"][0] and br["none"][0] and br["none"][1] else "—",
        REP_ROWS=rep_rows or '<tr><td colspan="4" class="muted">Повторів після відповіді не знайдено.</td></tr>',
        REP_N=str(len(rep)), REP_F=str(sum(len(f) for _, f in rep)),
        G_BARS=g_bars, GS_ROWS=gs_rows,
        G_TOP=e(short_cat(gc[0][0])) if gc else "—", G_TOP_P=pc(gc[0][1], gc[0][1] + gc[0][2], 0) if gc else "—",
        G_LOW=e(short_cat(gc[-1][0])) if gc else "—", G_LOW_P=pc(gc[-1][1], gc[-1][1] + gc[-1][2], 0) if gc else "—",
    )


def media_timeline(row: dict, W=600, H=70) -> str:
    """Підписи по днях (стовпчики) + позначки статей (▼) + лінія дня набору 25 000. Одна вісь Y."""
    days = row["daily"]
    last = max([d for d, v in enumerate(days) if v] + [int(row.get("reached_day") or 0) + 5] + row["art_days"] + [10])
    last = min(len(days) - 1, last + 2)
    vals = days[: last + 1]
    mx = max(vals) or 1
    bw = W / len(vals)
    # ряди під віссю: ▲ статті (H+22) і номери днів (H+36) — окремо, щоб не перекривались
    out = [f'<svg viewBox="0 0 {W} {H+40}" class="chart" role="img" aria-label="Підписи по днях і статті">',
           f'<line x1="0" x2="{W}" y1="{H+12}" y2="{H+12}" class="axis"/>']
    for i, v in enumerate(vals):
        h = (H - 4) * v / mx
        if h:
            out.append(f'<rect x="{i*bw+0.5:.1f}" y="{H+12-h:.1f}" width="{max(1, bw-1):.1f}" height="{h:.1f}" class="m1"><title>день {i}: {fmt(v)}</title></rect>')
    if row.get("reached_day") is not None and row["reached_day"] <= last:
        x = row["reached_day"] * bw
        out.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="6" y2="{H+12}" class="refl"/><text x="{x+3:.1f}" y="12" class="tl" text-anchor="start">25 000</text>')
    for d, c in collections.Counter(max(0, d_) for d_ in row["art_days"]).items():  # статті до старту — на день 0
        if 0 <= d <= last:
            x = d * bw + bw / 2
            out.append(f'<text x="{x:.1f}" y="{H+22}" class="mk" text-anchor="middle">▲{c if c > 1 else ""}</text>')
    for d in range(0, last + 1, 7 if last > 20 else 2):
        out.append(f'<text x="{d*bw+bw/2:.1f}" y="{H+36}" class="tl" style="font-size:6.5px">{d}</text>')
    out.append("</svg>")
    return "".join(out)


def media_context(r: SiteReport) -> dict[str, str]:
    rows = r.media or []
    if not rows:
        return dict(M_LEAD="Медіааналіз не виконувався (запуск з --no-media або без мережі).", M_TILES="", M_ROWS="",
                    M_TIMELINES="", M_SOURCES="—")
    cov = [x for x in rows if x["articles"]]
    arts = sum(len(x["articles"]) for x in rows)
    before = sum(x["before"] for x in rows); after = sum(x["after"] for x in rows)
    src = collections.Counter()
    for x in rows:
        src.update(x["sources"])
    def day(v):
        return "—" if v is None else f"{dec(max(0, v), 0)}-й"
    trs = ""
    for x in sorted(rows, key=lambda x: -(x["p"].get("signaturesNumber") or 0)):
        p = x["p"]
        top = ", ".join(e(k) for k, _ in x["sources"].most_common(3)) or '<span class="muted">не знайдено</span>'
        trs += (f'<tr><td class="num id">{p["id"]}</td><td class="ttl">{e(plain(p.get("title")))}</td>'
                f'<td class="num">{fmt(p.get("signaturesNumber") or 0)}</td>'
                f'<td class="num">{x["before"]} / {x["after"]}</td><td class="num">{day(x["first_day"])}</td>'
                f'<td class="num">{day(x["reached_day"])}</td><td class="ttl" style="font-size:7.6pt">{top}</td></tr>')
    tl = ""
    for x in sorted(cov, key=lambda x: -len(x["articles"]))[:4]:
        p = x["p"]
        heads = "".join(f'<li><b>{x_["date"][8:10]}.{x_["date"][5:7]}</b> {e(x_["source"])} — {e(x_["title"])[:100]}</li>'
                        for x_ in x["articles"][:3])
        tl += (f'<div class="card" style="margin-top:8px"><h3>№ {p["id"]} · {e(plain(p.get("title")))[:95]}</h3>'
               f'{media_timeline(x, H=56)}<ul class="peaks">{heads}</ul></div>')
    lead = (f"З {len(rows)} найбільших петицій і «магнітів» згадки в Google News знайдено для {len(cov)}. "
            f"Із {arts} статей лише {before} вийшли <b>до</b> набору 25 000 — решта ({after}) переказують новину "
            f"«петиція набрала…». Тобто медіа здебільшого фіксують успіх, а не створюють його.")
    if before > after:
        lead = (f"З {len(rows)} найбільших петицій і «магнітів» згадки в Google News знайдено для {len(cov)}. "
                f"Більшість статей ({before} з {arts}) вийшли до набору 25 000 — медіа допомагали збирати підписи.")
    tiles = (f'<div class="tile"><b class="num">{len(cov)} з {len(rows)}</b><span>петицій зі згадками в медіа</span></div>'
             f'<div class="tile"><b class="num">{arts}</b><span>релевантних статей</span></div>'
             f'<div class="tile"><b class="num">{pc(before, arts, 0) if arts else "—"}%</b><span>статей до набору 25 000</span></div>'
             f'<div class="tile"><b class="num">{e(src.most_common(1)[0][0]) if src else "—"}</b><span>найчастіше джерело</span></div>')
    return dict(M_LEAD=lead, M_TILES=tiles, M_ROWS=trs, M_TIMELINES=tl,
                M_SOURCES=", ".join(f"{e(k)} ({v})" for k, v in src.most_common(8)) or "—")


def doc_title(r: SiteReport) -> str:
    return f"Петиції до Кабміну — загальний огляд {r.generated_at:%Y-%m-%d}"


def render_html(r: SiteReport, font: str = DEFAULT_FONT) -> str:
    tpl = resources.files("petistat").joinpath("templates/site.html").read_text(encoding="utf-8")
    ctx = build_context(r) | dynamics_context(r) | extra_context(r) | authors_context(r) | wrong_context(r) | more_context(r) | media_context(r) | font_ctx(font)
    ctx["CSS"] = load_css(ctx)
    ctx["DOC_TITLE"] = e(doc_title(r))
    out = re.sub(r"\{\{(\w+)\}\}", lambda m: ctx.get(m.group(1), m.group(0)), tpl)
    missing = sorted(set(re.findall(r"\{\{(\w+)\}\}", out)))
    if missing:
        raise KeyError(f"Незаповнені плейсхолдери: {missing}")
    return out


def summary(r: SiteReport) -> dict:
    return {
        "generated_at": r.generated_at.isoformat(timespec="seconds"),
        "petitions": r.n, "signatures": r.total_sig, "status": r.status,
        "passed_25k": len(r.passed), "answered": len(r.answered), "answered_below_threshold": r.answered_below,
        "answer_delay_days": {"median": statistics.median(r.answer_delays) if r.answer_delays else None,
                              "values": sorted(round(x, 2) for x in r.answer_delays)},
        "days_to_25k": {"median": statistics.median(r.to_25k) if r.to_25k else None,
                        "values": sorted(round(x, 2) for x in r.to_25k)},
        "years": [{"year": y.year, "petitions": y.n, "passed": y.passed, "answered": y.answered,
                   "median_signatures": statistics.median(y.sigs), "authors_F": y.authors_F, "authors_M": y.authors_M}
                  for y in r.years],
        "categories": [{"id": c.id, "title": c.title, "petitions": c.n, "signatures": c.sig, "passed": c.passed,
                        "signers_women": c.sF, "signers_total": c.sN, "signers_caps": c.sU} for c in r.cats],
        "authors": {"F": r.authors_F, "M": r.authors_M, "org": r.authors_org, "unique": r.authors_unique,
                    "passed_by_gender": r.passed_by_author_gender, "regions": r.regions, "prolific": r.prolific},
        "signers": {"petitions": r.sig_petitions, "signatures": r.sig_rows, "unique": r.unique_signers,
                    "women_signatures": r.sF, "men_signatures": r.sM, "women_unique": r.uF, "men_unique": r.uM,
                    "caps_unique": r.u_upper, "na_patronymic": r.u_na, "by_year": r.by_year_signers,
                    "women_by_author_gender": r.women_by_author, "hours": r.hours, "weekdays": r.weekdays,
                    "repeat": r.repeat, "repeat_max": r.repeat_max, "top1_share": r.top1_share,
                    "top10_share": r.top10_share, "names_F": r.names_F, "names_M": r.names_M,
                    "surnames": r.surnames, "missing_petitions": r.missing_signers},
    }


def write_outputs(r: SiteReport, out_dir="reports", pdf=True, font: str = DEFAULT_FONT) -> dict[str, Path]:
    d = Path(out_dir) / "kmu-all"
    d.mkdir(parents=True, exist_ok=True)
    stamp = r.generated_at.strftime("%Y-%m-%d_%H%M%S")
    paths = {"html": d / f"{stamp}.html", "json": d / f"{stamp}.json"}
    paths["html"].write_text(render_html(r, font), encoding="utf-8")
    paths["json"].write_text(json.dumps(summary(r), ensure_ascii=False, indent=1), encoding="utf-8")
    if pdf:
        p = d / f"{stamp}.pdf"
        if to_pdf(paths["html"], p):
            paths["pdf"] = p
    # копія з людською назвою: «Петиції до Кабміну — загальний огляд 2026-11-13.pdf» (JSON — лише технічний, без копії)
    for k in ("html", "pdf"):
        if k in paths:
            nice = d / f"{doc_title(r)}.{k}"
            shutil.copyfile(paths[k], nice)
            paths[f"{k}_named"] = nice
    return paths
