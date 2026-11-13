"""Рендер звіту: HTML (A4, 3 сторінки) + опційно PDF через headless Chromium."""
from __future__ import annotations

import html
import json
import re
import shutil
import subprocess
from importlib import resources
from pathlib import Path

from .analysis import THRESHOLD, Report, to_kyiv

e = html.escape


# ------------------------------------------------------------------ формати
def fmt(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def dec(v: float, d: int = 1) -> str:
    return f"{v:.{d}f}".replace(".", ",")


def pct(a: float, b: float, d: int = 1) -> str:
    return dec(100 * a / b, d) if b else "0"


def dmy(iso: str) -> str:
    return f"{iso[8:10]}.{iso[5:7]}.{iso[:4]}"


def plural(n: int, one: str, few: str, many: str) -> str:
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return one
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return few
    return many


def name_case(s: str) -> str:
    """'БРИНЗА' → 'Бринза', 'ДЕМ'ЯН' → 'Дем'ян', 'ІВАНО-ФРАНКІВСЬКА' → 'Івано-Франківська'."""
    return "-".join(w[:1].upper() + w[1:].lower() for w in (s or "").strip().split("-"))


def author_names(a: dict) -> tuple[str, str, bool]:
    last, first, pat = (a.get(k) or "" for k in ("lastName", "firstName", "patronymic"))
    full = " ".join(name_case(x) for x in (last, first, pat) if x and x.lower() != "n/a")
    short = name_case(last) + " " + " ".join(f"{name_case(x)[:1]}." for x in (first, pat) if x and x.lower() != "n/a")
    letters = [c for c in last + first if c.isalpha()]
    is_caps = bool(letters) and all(c.isupper() for c in letters)
    return full or "невідомий автор", short.strip() or "автора", is_caps


def slugify(s: str) -> str:
    tr = dict(zip("абвгґдеєжзиіїйклмнопрстуфхцчшщьюя'",
                  ["a", "b", "v", "h", "g", "d", "e", "ie", "zh", "z", "y", "i", "i", "i", "k", "l", "m", "n", "o", "p",
                   "r", "s", "t", "u", "f", "kh", "ts", "ch", "sh", "shch", "", "iu", "ia", ""]))
    s = "".join(tr.get(c, c) for c in s.lower())
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-") or "author"


# ------------------------------------------------------------------ SVG / HTML-фрагменти
def svg_days(days: list[tuple[str, int]], today: str) -> str:
    dl = days[-10:] or [(today, 0)]
    dm = max(v for _, v in dl) or 1
    W, H = 300, 130
    bw = min(40, (W - 10) / len(dl) - 6)
    gap = (W - len(dl) * bw) / (len(dl) + 1)
    out = [f'<svg viewBox="0 0 {W} {H+30}" class="chart" role="img" aria-label="Підписи по днях">'
           f'<line x1="0" x2="{W}" y1="{H}" y2="{H}" class="axis"/>']
    for i, (d, v) in enumerate(dl):
        x = gap + i * (bw + gap)
        h = max(2, (H - 18) * v / dm)
        y = H - h
        lab = f"{d[8:10]}.{d[5:7]}" + (" *" if d == today else "")
        r = min(4, bw / 2)
        out.append(f'<g><title>{lab}: {v} підписів</title>'
                   f'<path d="M{x:.1f},{H} V{y+r:.1f} q0,-{r} {r},-{r} h{bw-2*r:.1f} q{r},0 {r},{r} V{H} Z" class="m1"/>'
                   f'<text x="{x+bw/2:.1f}" y="{y-5:.1f}" class="vl">{v}</text>'
                   f'<text x="{x+bw/2:.1f}" y="{H+15}" class="tl">{lab}</text></g>')
    out.append("</svg>")
    return "".join(out)


def svg_hours(hs: list[int]) -> str:
    hm = max(hs) or 1
    W, H = 300, 100
    bw = W / 24
    out = [f'<svg viewBox="0 0 {W} {H+22}" class="chart" role="img" aria-label="Підписи за годинами">'
           f'<line x1="0" x2="{W}" y1="{H}" y2="{H}" class="axis"/>']
    for h, v in enumerate(hs):
        hh = max(1.5, (H - 6) * v / hm)
        out.append(f'<g><title>{h:02d}:00–{h:02d}:59 — {v}</title><rect x="{h*bw+1:.1f}" y="{H-hh:.1f}" '
                   f'width="{bw-2:.1f}" height="{hh:.1f}" rx="2" class="m1"/></g>')
    out += [f'<text x="{h*bw+bw/2:.1f}" y="{H+14}" class="tl">{h:02d}</text>' for h in (0, 6, 12, 18, 23)]
    out.append("</svg>")
    return "".join(out)


def gender_bar(label: str, f: float, note: str) -> str:
    return (f'<div class="gb"><div class="gbl"><span>{e(label)}</span><span class="muted">{note}</span></div>'
            f'<div class="stack"><i class="f" style="width:{f:.2f}%" title="Жінки {dec(f)}%"></i>'
            f'<i class="m" style="width:{100-f:.2f}%" title="Чоловіки {dec(100-f)}%"></i></div>'
            f'<div class="gbv"><span>{dec(f)}%</span><span>{dec(100-f)}%</span></div></div>')


# Шрифти з кирилицею (Google Fonts). axes — специфікація осей для css2 API; тільки ті, що реально є у шрифта.
FONTS = {
    "inter":    ("Inter", "opsz,wght@14..32,400..800"),   # opsz: великі цифри/заголовки щільніші
    "overpass": ("Overpass", "wght@400..800"),
    "onest":    ("Onest", "wght@400..800"),
    "golos":    ("Golos Text", "wght@400..800"),
    "manrope":  ("Manrope", "wght@400..800"),
    "plex":     ("IBM Plex Sans", "wght@400;500;600;700"),
}
DEFAULT_FONT = "inter"


def font_ctx(key: str) -> dict[str, str]:
    family, axes = FONTS.get(key.lower(), FONTS[DEFAULT_FONT])
    url = f"https://fonts.googleapis.com/css2?family={family.replace(' ', '+')}:{axes}&display=swap"
    return {"FONT_URL": url, "FONT_STACK": f'"{family}",system-ui,sans-serif', "FONT_FEATURES": "normal"}


STATUS = {"Unsupported": "підтримку не набрала", "Supported": "набрала 25 000", "Answered": "є відповідь",
          "Approved": "збір підписів", "Published": "збір підписів"}
# Назви петицій не обрізаються. У HTML потрапляє до MAX_HTML_ROWS рядків, а скрипт у шаблоні
# (fitTable) після завантаження шрифтів ховає зайві рядки знизу, доки сторінка 1 не влізе в A4,
# і перераховує підсумковий рядок «…і ще N». Фокусна петиція не ховається ніколи.
MAX_HTML_ROWS = 30


def petitions_table(rep: Report) -> str:
    pets = rep.petitions
    mx = max((p.n for p in pets), default=1) or 1
    shown = pets[:MAX_HTML_ROWS]
    if rep.focus not in shown:
        shown = pets[:MAX_HTML_ROWS - 1] + [rep.focus]
    out = []
    for p in shown:
        focus = p is rep.focus
        attrs = f' data-n="{p.n}"' + (' class="hl" data-focus="1"' if focus else "")
        title = (p.meta.get("title") or "").strip()
        st = STATUS.get(p.meta.get("status"), p.meta.get("status") or "")
        started = f" · з {dmy(p.meta['startedAt'])}" if p.meta.get("startedAt") else ""
        f = 100 * p.F / p.n if p.n else 0
        u = 100 * p.upper / p.n if p.n else 0
        out.append(f'<tr{attrs}><td class="num id">{p.id}</td><td class="ttl">{e(title)}<span class="st">{st}{started}</span></td>'
                   f'<td class="barc"><div class="bar"><i style="width:{max(1.5, 100*p.n/mx):.1f}%"></i></div>'
                   f'<b class="num">{fmt(p.n)}</b></td><td class="num">{f:.0f}%</td><td class="num">{u:.0f}%</td></tr>')
    rest = [p for p in pets if p not in shown]
    cnt, total = len(rest), sum(p.n for p in rest)
    out.append(f'<tr class="more" data-count="{cnt}" data-sum="{total}"{"" if cnt else " hidden"}><td></td>'
               f'<td class="ttl muted">…і ще <span class="cnt">{cnt} {plural(cnt, "петиція", "петиції", "петицій")}</span></td>'
               f'<td class="barc"><b class="num sum">{fmt(total)}</b></td><td></td><td></td></tr>')
    return "".join(out)


# ------------------------------------------------------------------ тексти, що залежать від даних
def compare(a: float, b: float) -> str:
    if abs(a - b) < 2:
        return "майже така сама, як"
    return "вища, ніж" if a > b else "нижча, ніж"


def build_context(rep: Report) -> dict[str, str]:
    t, B, pets = rep.focus, rep.baseline, rep.petitions
    full, short, author_caps = author_names(rep.author)
    region = (rep.author.get("region") or {}).get("name")
    now = rep.generated_at
    today = now.strftime("%Y-%m-%d")
    npet = len(pets)
    bF = 100 * B.F / B.total if B.total else None
    bU = 100 * B.upper / B.total if B.total else None
    tU = 100 * t.upper / t.n if t.n else 0
    small = [100 * p.upper / p.n for p in pets if 0 < p.n <= 150]
    smallcaps = f"{min(small):.0f}–{max(small):.0f}%" if len(small) > 1 else (f"{small[0]:.0f}%" if small else "")

    # --- сторінка 1
    lead = f"Автор зареєстрував {npet} {plural(npet, 'петицію', 'петиції', 'петицій')} до Кабміну. "
    if npet >= 3 and rep.total:
        top2 = sum(p.n for p in pets[:2])
        lead += f"Дві найбільші з них зібрали {pct(top2, rep.total, 0)}% усіх підписів. "
    if rep.total:
        lead += f"Жінки становлять {pct(rep.total_F, rep.total, 0)}% підписантів."
    passed = [p for p in pets if p.n >= THRESHOLD or p.meta.get("status") in ("Supported", "Answered")]
    failed = [p for p in pets if p.meta.get("status") == "Unsupported"]
    p1 = ["Петиції до КМУ потрібно 25 000 підписів за 3 місяці."]
    p1.append("Поріг подолали: " + ", ".join(f"№ {p.id}" for p in passed) + "." if passed
              else "Жодна петиція автора поки не набрала цього порогу.")
    if failed:
        p1.append("Закрилися без підтримки: " + ", ".join(f"№ {p.id} ({fmt(p.n)})" for p in failed[:5])
                  + (" та ін." if len(failed) > 5 else "") + ".")
    if smallcaps:
        p1.append(f"У малих петиціях (до 150 підписів) частка КАПСУ: {smallcaps}.")

    # --- сторінка 2
    if t.meta.get("status") in ("Supported", "Answered") or t.n >= THRESHOLD:
        prog = f"Поріг <b>25 000</b> подолано. У середньому ≈{rep.avg_per_day:.0f} підписів на день."
    elif t.meta.get("endedAt") and to_kyiv(t.meta["endedAt"]) < now:
        prog = f"Збір завершився {dmy(t.meta['endedAt'])}. Петиція не набрала порогу, бракувало <b class=\"num\">{fmt(THRESHOLD - t.n)}</b>."
    else:
        days_left = max(1, (to_kyiv(t.meta["endedAt"]).date() - now.date()).days) if t.meta.get("endedAt") else 90
        left = THRESHOLD - t.n
        prog = (f"Бракує <b class=\"num\">{fmt(left)}</b> підписів за {days_left} "
                f"{plural(days_left, 'день', 'дні', 'днів')}, тобто потрібно ≈<b class=\"num\">{left/days_left:.0f}</b> на день. "
                f"Поки що в середньому ≈{rep.avg_per_day:.0f} на день.")
    if rep.second:
        ovl = (f"{fmt(rep.overlap_second)} підписантів {t.id} також підписали петицію {rep.second.id}. "
               f"Загалом {fmt(rep.overlap_any)} з них підписували й інші петиції автора.")
    else:
        ovl = "Інших петицій у автора немає, тож перетин аудиторій не рахувався."
    last_day = rep.days[-1][0] if rep.days else None
    days_note = (f"* {last_day[8:10]}.{last_day[5:7]} — неповний день (зріз о {now:%H:%M})."
                 if last_day == today else "Показано останні 10 днів збору (час київський).")
    gbars = gender_bar(f"Петиція {t.id}", 100 * t.F / t.n if t.n else 0, f"{fmt(t.n)} підписантів")
    if npet > 1:
        gbars += gender_bar(f"Усі {npet} {plural(npet, 'петиція', 'петиції', 'петицій')} автора",
                            100 * rep.total_F / rep.total if rep.total else 0, f"{fmt(rep.total)} підписів")
    if bF is not None:
        gbars += gender_bar(f"Еталон: {len(B.petitions)} великих петицій КМУ", bF, f"{fmt(B.total)} підписів")

    # --- сторінка 3
    caps_rows = [(f"Петиція {t.id}", tU)]
    if rep.second:
        caps_rows.append((f"Петиція {rep.second.id}", 100 * rep.second.upper / max(1, rep.second.n)))
        rest = [p for p in pets if p not in (t, rep.second)]
        if rest and sum(p.n for p in rest):
            caps_rows.append((f"Решта {len(rest)} {plural(len(rest), 'петиція', 'петиції', 'петицій')} автора",
                              100 * sum(p.upper for p in rest) / sum(p.n for p in rest)))
    if bU is not None:
        caps_rows.append((f"Еталон: {len(B.petitions)} великих петицій КМУ", bU))
    cm = max(30.0, max(v for _, v in caps_rows) * 1.1)
    capsbars = "".join(f'<div class="cr"><span class="crl">{e(l)}</span><div class="crb"><i style="width:{100*v/cm:.1f}%">'
                       f'</i></div><b class="num">{dec(v)}%</b></div>' for l, v in caps_rows)
    caps_note = f"Частка КАПСУ на петиції {t.id} ({dec(tU)}%)"
    caps_note += (f" {compare(tU, bU)} на великих петиціях КМУ ({dec(bU)}%)." if bU is not None else ".")
    if smallcaps:
        caps_note += f" У малих петиціях автора: {smallcaps}."
    if author_caps:
        caps_note += f" Сам автор теж записаний КАПСОМ: «{e(' '.join((rep.author.get(k) or '') for k in ('lastName', 'firstName', 'patronymic')))}»."
    na_note = ("<b>«n/a» у по батькові.</b> " +
               (f"У петиціях автора є {rep.na} {plural(rep.na, 'підписант', 'підписанти', 'підписантів')} "
                "із по батькові «n/a» (часто це іноземці). Це прямий слід BankID. Якщо частина з них записана звичайними "
                "літерами, значить, регістр залежить від конкретного банку." if rep.na else
                "У петиціях цього автора таких записів немає, але на сайті загалом вони трапляються."))
    if rep.quintiles:
        lo, hi = min(rep.quintiles), max(rep.quintiles)
        q_note = (("<b>КАПС майже не залежить від «віку» облікового запису.</b> " if hi - lo < 5 else
                   "<b>КАПС різниться залежно від «віку» облікового запису.</b> ")
                  + f"У п'яти рівних групах за ID акаунта частка КАПСУ становить {dec(lo)}–{dec(hi)}%.")
    else:
        q_note = "<b>«Вік» облікового запису:</b> замало підписантів для порівняння за групами ID."
    if rep.age_old is not None and rep.age_young is not None:
        diff = rep.age_old - rep.age_young
        head = ("Зв'язок із віком людини слабкий." if abs(diff) < 5 else
                "Помітний зв'язок із віком людини." if diff > 0 else "Зв'язку зі старшим віком не видно.")
        age_note = (f"<b>{head}</b> Серед імен, поширених у старшого покоління (Галина, Надія, Василь, Степан…), "
                    f"КАПСОМ записано {dec(rep.age_old)}%. Серед «молодих» імен (Софія, Дарина, Максим, Артем…) — {dec(rep.age_young)}%.")
    else:
        age_note = "<b>Вік людини:</b> замало підписантів з характерними іменами для порівняння."

    s = rep.surname
    sn = max(1, sum(s.values()))
    tF, tM = t.F, t.M
    g_note = f"Чоловіків серед підписантів {t.id}: {pct(tM, t.n)}%"
    g_note += (f", у середньому по еталонній вибірці: {dec(100 - bF)}%." if bF is not None else ".")
    cf, cmm = (100 * rep.caps_F / tF if tF else 0), (100 * rep.caps_M / tM if tM else 0)
    if min(tF, tM) < 20:
        capsg_note = f"Одна зі статей представлена слабко ({min(tF, tM)} осіб), тому порівняння ненадійне."
    elif abs(cf - cmm) < 1.5:
        capsg_note = f"Частка КАПСУ майже однакова: {rep.caps_F} жінок і {rep.caps_M} чоловіків."
    else:
        who = "жінок" if cf > cmm else "чоловіків"
        capsg_note = f"Серед {who} КАПС частіший: {rep.caps_F} жінок і {rep.caps_M} чоловіків."
    ctx = dict(
        AUTHOR=e(full), AUTHOR_SHORT=e(short), REGION=f" · {e(region)}" if region else "",
        LEAD=lead, P1_NOTE=" ".join(p1), ROWS=petitions_table(rep),
        NOW=f"{now:%d.%m.%Y, %H:%M}", NOWT=f"{now:%H:%M}",
        NPET=str(npet), TOT=fmt(rep.total), UNIQ=fmt(rep.unique),
        FALL=pct(rep.total_F, rep.total, 0), UALL=pct(rep.total_upper, rep.total, 0),
        T_ID=str(t.id), T_CODE=e(t.meta.get("code") or str(t.id)), T_TITLE=e((t.meta.get("title") or "").strip()),
        T_START=dmy(t.meta["startedAt"]) if t.meta.get("startedAt") else "—",
        T_END=dmy(t.meta["endedAt"]) if t.meta.get("endedAt") else "—",
        T_N=fmt(t.n), T_F=fmt(tF), T_M=fmt(tM), T_FP=pct(tF, t.n), T_MP=pct(tM, t.n),
        B_M=dec(100 - bF) if bF is not None else "—", B_N=fmt(B.total), B_K=str(len(B.petitions)),
        B_SINCE=dmy(B.since) if B.since else "—",
        B_RANGE=f"{dec(min(B.sizes)/1000)}–{dec(max(B.sizes)/1000)}" if B.sizes else "—",
        GBARS=gbars, DAYS=svg_days(rep.days, today), HOURS=svg_hours(rep.hours), DAYS_NOTE=days_note,
        S_F=str(s["F"]), S_M=str(s["M"]), S_N=fmt(s["N"]), S_NP=pct(s["N"], sn), S_DP=pct(s["F"] + s["M"], sn, 0),
        S_FP=pct(s["F"], s["F"] + s["M"]), S_WF=f"{100*s['F']/sn:.2f}", S_WM=f"{100*s['M']/sn:.2f}",
        S_WN=f"{100*s['N']/sn:.2f}", S_OK=str(rep.surname_ok),
        PROG=f"{min(100, 100*t.n/THRESHOLD):.2f}", PROGP=pct(min(t.n, THRESHOLD), THRESHOLD), PROG_TEXT=prog,
        OVL_NOTE=ovl,
        FEM="".join(f'<li><span>{e(n)}</span><b class="num">{c}</b></li>' for n, c in rep.top_names_F),
        MAL="".join(f'<li><span>{e(n)}</span><b class="num">{c}</b></li>' for n, c in rep.top_names_M),
        CAPSBARS=capsbars, CAPS_NOTE=caps_note, NA_NOTE=na_note, Q_NOTE=q_note, AGE_NOTE=age_note,
        G_NOTE=g_note, CAPSG_NOTE=capsg_note,
        CAPS_FP=pct(rep.caps_F, tF), CAPS_MP=pct(rep.caps_M, tM),
        CAPS_FW=f"{min(100, 100*rep.caps_F/max(1, tF)/30*100):.1f}",
        CAPS_MW=f"{min(100, 100*rep.caps_M/max(1, tM)/30*100):.1f}",
    )
    return ctx


def render_html(rep: Report, template: str | Path | None = None, font: str = DEFAULT_FONT) -> str:
    if template:
        tpl = Path(template).read_text(encoding="utf-8")
    else:
        tpl = resources.files("petistat").joinpath("templates/report.html").read_text(encoding="utf-8")
    ctx = build_context(rep) | font_ctx(font)
    out = re.sub(r"\{\{(\w+)\}\}", lambda m: ctx.get(m.group(1), m.group(0)), tpl)
    missing = sorted(set(re.findall(r"\{\{(\w+)\}\}", out)))
    if missing:
        raise KeyError(f"Незаповнені плейсхолдери в шаблоні: {missing}")
    return out


def summary(rep: Report) -> dict:
    """Ключові цифри для JSON-вивантаження і для таблиці в TUI (без персональних даних підписантів)."""
    t, B = rep.focus, rep.baseline
    full, _, _ = author_names(rep.author)
    return {
        "generated_at": rep.generated_at.isoformat(timespec="seconds"),
        "author": {"id": rep.author.get("id"), "name": full, "region": (rep.author.get("region") or {}).get("name")},
        "focus": {"id": t.id, "title": t.meta.get("title"), "signatures": t.n, "women": t.F, "men": t.M,
                  "caps": t.upper, "surname_gender": rep.surname, "surname_correct": rep.surname_ok},
        "petitions": [{"id": p.id, "title": p.meta.get("title"), "status": p.meta.get("status"),
                       "signatures": p.n, "women": p.F, "caps": p.upper} for p in rep.petitions],
        "totals": {"signatures": rep.total, "unique": rep.unique, "women": rep.total_F, "caps": rep.total_upper},
        "baseline": {"petitions": [p["id"] for p in B.petitions], "signatures": B.total, "women": B.F, "caps": B.upper},
        "caps_by_gender": {"F": rep.caps_F, "M": rep.caps_M}, "na_patronymic": rep.na,
        "caps_by_account_quintile": rep.quintiles, "caps_old_names": rep.age_old, "caps_young_names": rep.age_young,
    }


def find_browser() -> str | None:
    for b in ("chromium", "chromium-browser", "google-chrome-stable", "google-chrome", "brave", "microsoft-edge"):
        if p := shutil.which(b):
            return p
    return None


def to_pdf(html_path: Path, pdf_path: Path) -> bool:
    br = find_browser()
    if not br:
        return False
    subprocess.run([br, "--headless=new", "--disable-gpu", "--no-pdf-header-footer", "--virtual-time-budget=15000", "--window-size=1280,1800",
                    f"--print-to-pdf={pdf_path.resolve()}", html_path.resolve().as_uri()],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
    return pdf_path.exists()


def write_outputs(rep: Report, out_dir: str | Path = "reports", pdf: bool = True,
                  template: str | Path | None = None, font: str = DEFAULT_FONT) -> dict[str, Path]:
    """reports/<автор>-<id>/<дата_час>.{html,pdf,json} + latest.* (попередні звіти не видаляються)."""
    _, short, _ = author_names(rep.author)
    d = Path(out_dir) / f"{slugify(short)}-{rep.focus.id}"
    d.mkdir(parents=True, exist_ok=True)
    stamp = rep.generated_at.strftime("%Y-%m-%d_%H%M%S")
    paths = {"html": d / f"{stamp}.html", "json": d / f"{stamp}.json"}
    paths["html"].write_text(render_html(rep, template, font), encoding="utf-8")
    paths["json"].write_text(json.dumps(summary(rep), ensure_ascii=False, indent=1), encoding="utf-8")
    if pdf:
        p = d / f"{stamp}.pdf"
        if to_pdf(paths["html"], p):
            paths["pdf"] = p
    for k, p in list(paths.items()):
        latest = d / f"latest.{k}"
        shutil.copyfile(p, latest)
    return paths
