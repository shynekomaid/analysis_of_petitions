#!/usr/bin/env python3
"""Регенерація інфографіки по петиціях автора на petition.kmu.gov.ua.

Кроки:
  1. завантажує список усіх петицій і відбирає петиції автора (за creatorId);
  2. завантажує підписантів кожної петиції автора;
  3. завантажує еталонну вибірку (великі петиції інших авторів) для порівняння;
  4. рахує стать (за по батькові), регістр ПІБ (КАПС), динаміку тощо;
  5. підставляє цифри в template.html → HTML (і за бажанням PDF через Chromium).

Лише стандартна бібліотека Python 3.9+.
"""
import argparse, collections, datetime as dt, html, json, os, re, shutil, subprocess, sys, time
import urllib.parse, urllib.request

API = "https://petition.kmu.gov.ua/api"
UA = {"User-Agent": "Mozilla/5.0 (petition-stats script)"}
KYIV = dt.timezone(dt.timedelta(hours=2))  # листопад–березень: UTC+2

# ---------------------------------------------------------------- HTTP / кеш
def get(path, params=None, retries=3):
    url = f"{API}/{path}" + ("?" + urllib.parse.urlencode(params) if params else "")
    for i in range(retries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120) as r:
                return json.load(r)
        except Exception as e:
            if i == retries - 1:
                raise
            print(f"  повтор {url}: {e}", file=sys.stderr); time.sleep(2 * (i + 1))

def cached(cache, name, offline, fn):
    p = os.path.join(cache, name)
    if offline:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    data = fn()
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return data

def fetch_all_petitions():
    rows, start = [], 0
    while True:
        d = get("petitions", {"start": start, "count": 500, "sortField": "startedAt", "sortDirection": "desc",
                              "fields": "id,title,code,status,signaturesNumber,creatorId,startedAt,createdAt,endedAt"})
        rows += d["rows"]; start += len(d["rows"])
        print(f"  петиції: {start}/{d['count']}", file=sys.stderr)
        if not d["rows"] or start >= d["count"]:
            return rows
        time.sleep(0.3)

def fetch_signatories(pid):
    d = get(f"petitions/{pid}/signatories")
    print(f"  підписанти {pid}: {len(d['rows'])}", file=sys.stderr)
    time.sleep(0.3)
    return d["rows"]

# ---------------------------------------------------------------- класифікація
def gender(sg):
    p = (sg.get("patronymic") or "").strip().lower()
    if re.search(r"(ич|огли|оглу)$", p): return "M"
    if re.search(r"(на|кизи|гизи)$", p): return "F"
    f = (sg.get("firstName") or "").strip().lower()  # запасний варіант: ім'я
    if f.endswith(("а", "я")) and f not in ("микола", "ілля", "кузьма", "фома", "лука", "сава"): return "F"
    return "M" if f else "U"

def gender_by_surname(last):
    l = (last or "").strip().lower().split("-")[0]
    if re.search(r"(ська|цька|зька|ова|ева|єва|іна|їна)$", l): return "F"
    if re.search(r"(ський|цький|зький|ов|ев|єв)$", l): return "M"
    return None  # -енко, -чук, -ук, -ишин, -ак… — однакові для обох статей

def case_kind(sg):
    kinds = []
    for k in ("lastName", "firstName", "patronymic"):
        v = sg.get(k) or ""
        if v.strip().lower() == "n/a": continue
        L = [c for c in v if c.isalpha()]
        if L: kinds.append("upper" if all(c.isupper() for c in L) else "lower" if all(c.islower() for c in L) else "title")
    if kinds and all(k == "upper" for k in kinds): return "upper"
    if kinds and all(k == "title" for k in kinds): return "title"
    return "mixed"

OLD_NAMES = set("Галина Ганна Надія Любов Віра Ніна Валентина Лідія Раїса Тамара Зінаїда Ярослава Стефанія "
                "Василь Іван Петро Степан Михайло Микола Ярослав Мирослав Григорій Федір".split())
YOUNG_NAMES = set("Анастасія Дарина Софія Вікторія Діана Аліна Яна Христина Каріна Валерія "
                  "Дмитро Максим Владислав Артем Денис Назар Євген Ростислав".split())

# ---------------------------------------------------------------- форматування
fmt = lambda n: f"{n:,}".replace(",", " ")
dec = lambda v, d=1: f"{v:.{d}f}".replace(".", ",")
pct = lambda a, b, d=1: dec(100 * a / b, d) if b else "0"
dmy = lambda iso: f"{iso[8:10]}.{iso[5:7]}.{iso[:4]}"
e = html.escape

# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--creator", type=int, default=2176678, help="creatorId автора (Бринза Д. П. = 2176678)")
    ap.add_argument("--focus", type=int, default=11247, help="id петиції для детальної сторінки")
    ap.add_argument("--baseline", type=int, default=12, help="скільки великих петиції інших авторів брати як еталон (0 — вимкнути)")
    ap.add_argument("--baseline-since", default="2026-06-01", help="еталон: петиції, що стартували з цієї дати")
    ap.add_argument("--template", default="template.html")
    ap.add_argument("--out", default="brynza-petitions.html")
    ap.add_argument("--pdf", action="store_true", help="також зробити PDF (потрібен chromium / google-chrome)")
    ap.add_argument("--cache", default="data", help="тека для сирих JSON")
    ap.add_argument("--offline", action="store_true", help="не ходити в мережу, взяти дані з --cache")
    a = ap.parse_args()
    here = os.path.dirname(os.path.abspath(__file__))
    tpl_path = a.template if os.path.isabs(a.template) else os.path.join(here, a.template)
    os.makedirs(a.cache, exist_ok=True)
    C = lambda name, fn: cached(a.cache, name, a.offline, fn)

    # 1. петиції
    print("1/4 список петицій…", file=sys.stderr)
    allp = C("all_petitions.json", fetch_all_petitions)
    mine = sorted([p for p in allp if p.get("creatorId") == a.creator], key=lambda p: -p["signaturesNumber"])
    if not mine: sys.exit(f"Петицій автора creatorId={a.creator} не знайдено")
    if a.focus not in {p["id"] for p in mine}: sys.exit(f"Петиція {a.focus} не належить автору {a.creator}")

    # 2. підписанти петицій автора
    print("2/4 підписанти автора…", file=sys.stderr)
    S = {p["id"]: C(f"sig_{p['id']}.json", lambda pid=p["id"]: fetch_signatories(pid)) for p in mine}
    for rows in S.values():
        for r in rows:
            r["_g"] = gender(r["signatory"]); r["_c"] = case_kind(r["signatory"])

    # 3. еталон
    print("3/4 еталонна вибірка…", file=sys.stderr)
    base = [p for p in allp if p.get("creatorId") != a.creator and 300 <= p["signaturesNumber"] <= 6000
            and p["startedAt"] >= a.baseline_since]
    base = sorted(base, key=lambda p: -p["signaturesNumber"])[: a.baseline]
    B = dict(total=0, upper=0, fem=0, sizes=[])
    for p in base:
        rows = C(f"sig_{p['id']}.json", lambda pid=p["id"]: fetch_signatories(pid))
        B["total"] += len(rows); B["sizes"].append(len(rows))
        B["upper"] += sum(case_kind(r["signatory"]) == "upper" for r in rows)
        B["fem"] += sum(gender(r["signatory"]) == "F" for r in rows)

    # 4. метрики
    print("4/4 аналіз і рендер…", file=sys.stderr)
    P = {}
    for p in mine:
        rows = S[p["id"]]
        P[p["id"]] = dict(p, n=len(rows), F=sum(r["_g"] == "F" for r in rows),
                          upper=sum(r["_c"] == "upper" for r in rows))
    pets = sorted(P.values(), key=lambda p: -p["n"])
    tot = sum(p["n"] for p in pets); totF = sum(p["F"] for p in pets); totU = sum(p["upper"] for p in pets)
    uniq = {r["signatory"]["id"]: r for rows in S.values() for r in rows}
    t = P[a.focus]; trows = S[a.focus]
    now = dt.datetime.now(KYIV)

    # таблиця петицій
    mx = max(p["n"] for p in pets) or 1
    rows_html = ""
    for p in pets:
        hl = ' class="hl"' if p["id"] == a.focus else ""
        title = p["title"].strip(); title = title if len(title) <= 78 else title[:76].rstrip() + "…"
        st = {"Unsupported": "підтримку не набрала", "Supported": "набрала 25 000",
              "Answered": "є відповідь"}.get(p["status"], "збір підписів")
        f = 100 * p["F"] / p["n"] if p["n"] else 0; u = 100 * p["upper"] / p["n"] if p["n"] else 0
        rows_html += (f'<tr{hl}><td class="num id">{p["id"]}</td><td class="ttl">{e(title)}<span class="st">{st} · з {dmy(p["startedAt"])}</span></td>'
                      f'<td class="barc"><div class="bar"><i style="width:{max(1.5, 100*p["n"]/mx):.1f}%"></i></div><b class="num">{fmt(p["n"])}</b></td>'
                      f'<td class="num">{f:.0f}%</td><td class="num">{u:.0f}%</td></tr>')

    # графік по днях (за київським часом)
    def kyiv(iso): return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(KYIV)
    days = collections.Counter(kyiv(r["signedAt"]).strftime("%Y-%m-%d") for r in trows)
    dl = sorted(days.items())[-10:]; dm = max(v for _, v in dl)
    W, H = 300, 130; bw = min(40, (W - 10) / len(dl) - 6); gap = (W - len(dl) * bw) / (len(dl) + 1)
    last = dl[-1][0]
    dsvg = f'<svg viewBox="0 0 {W} {H+30}" class="chart" role="img" aria-label="Підписи по днях"><line x1="0" x2="{W}" y1="{H}" y2="{H}" class="axis"/>'
    for i, (d, v) in enumerate(dl):
        x = gap + i * (bw + gap); h = max(2, (H - 18) * v / dm); y = H - h
        lab = f"{d[8:10]}.{d[5:7]}" + (" *" if d == now.strftime("%Y-%m-%d") else "")
        dsvg += (f'<g><title>{lab}: {v} підписів</title><path d="M{x:.1f},{H} V{y+4:.1f} q0,-4 4,-4 h{bw-8:.1f} q4,0 4,4 V{H} Z" class="m1"/>'
                 f'<text x="{x+bw/2:.1f}" y="{y-5:.1f}" class="vl">{v}</text><text x="{x+bw/2:.1f}" y="{H+15}" class="tl">{lab}</text></g>')
    dsvg += "</svg>"

    # графік по годинах
    hs = [0] * 24
    for r in trows: hs[kyiv(r["signedAt"]).hour] += 1
    hm = max(hs) or 1; W2, H2 = 300, 100; bw2 = W2 / 24
    hsvg = f'<svg viewBox="0 0 {W2} {H2+22}" class="chart" role="img" aria-label="Підписи за годинами"><line x1="0" x2="{W2}" y1="{H2}" y2="{H2}" class="axis"/>'
    for h, v in enumerate(hs):
        hh = max(1.5, (H2 - 6) * v / hm)
        hsvg += f'<g><title>{h:02d}:00–{h:02d}:59 — {v}</title><rect x="{h*bw2+1:.1f}" y="{H2-hh:.1f}" width="{bw2-2:.1f}" height="{hh:.1f}" rx="2" class="m1"/></g>'
    for h in (0, 6, 12, 18, 23):
        hsvg += f'<text x="{h*bw2+bw2/2:.1f}" y="{H2+14}" class="tl">{h:02d}</text>'
    hsvg += "</svg>"

    # смуги статі
    def gbar(label, f, note):
        return (f'<div class="gb"><div class="gbl"><span>{label}</span><span class="muted">{note}</span></div>'
                f'<div class="stack"><i class="f" style="width:{f:.2f}%" title="Жінки {dec(f)}%"></i><i class="m" style="width:{100-f:.2f}%" title="Чоловіки {dec(100-f)}%"></i></div>'
                f'<div class="gbv"><span>{dec(f)}%</span><span>{dec(100-f)}%</span></div></div>')
    bF = 100 * B["fem"] / B["total"] if B["total"] else 0
    bU = 100 * B["upper"] / B["total"] if B["total"] else 0
    gbars = gbar(f"Петиція {a.focus}", 100 * t["F"] / t["n"], f'{fmt(t["n"])} підписантів')
    gbars += gbar(f"Усі {len(pets)} петицій автора", 100 * totF / tot, f"{fmt(tot)} підписів")
    if B["total"]: gbars += gbar(f'Еталон: {len(base)} великих петицій КМУ', bF, f'{fmt(B["total"])} підписів')

    # стать за прізвищем
    sur = collections.Counter(); ok = 0
    for r in trows:
        g = gender_by_surname(r["signatory"]["lastName"]); sur[g or "N"] += 1
        ok += bool(g) and g == r["_g"]
    sF, sM, sN = sur["F"], sur["M"], sur["N"]; sn = sF + sM + sN

    # КАПС: порівняльні смуги
    big2 = pets[1]["id"] if pets[0]["id"] == a.focus else pets[0]["id"]
    rest = [p for p in pets if p["id"] not in (a.focus, big2)]
    caps_rows = [(f"Петиція {a.focus}", 100 * t["upper"] / t["n"]),
                 (f"Петиція {big2}", 100 * P[big2]["upper"] / max(1, P[big2]["n"]))]
    if rest: caps_rows.append((f"Решта {len(rest)} петицій автора", 100 * sum(p["upper"] for p in rest) / max(1, sum(p["n"] for p in rest))))
    if B["total"]: caps_rows.append((f"Еталон: {len(base)} великих петицій КМУ", bU))
    cm = max(30, max(v for _, v in caps_rows) * 1.1)
    capsbars = "".join(f'<div class="cr"><span class="crl">{l}</span><div class="crb"><i style="width:{100*v/cm:.1f}%"></i></div><b class="num">{dec(v)}%</b></div>' for l, v in caps_rows)
    capsF = sum(r["_c"] == "upper" and r["_g"] == "F" for r in trows)
    capsM = sum(r["_c"] == "upper" and r["_g"] == "M" for r in trows)
    tF, tM = t["F"], t["n"] - t["F"]

    small = [100 * p["upper"] / p["n"] for p in pets if 0 < p["n"] <= 150]
    smallcaps = f"{min(small):.0f}–{max(small):.0f}%" if small else "—"

    # КАПС за квінтилями id облікового запису
    ids = sorted(r["signatory"]["id"] for r in trows); qb = [ids[int(len(ids) * q)] for q in (.2, .4, .6, .8)]
    Q = collections.defaultdict(lambda: [0, 0])
    for r in trows:
        k = sum(r["signatory"]["id"] >= b for b in qb); Q[k][0] += 1; Q[k][1] += r["_c"] == "upper"
    qv = [100 * u / n for n, u in Q.values() if n]

    # вікові імена (по унікальних підписантах автора)
    def share(names):
        xs = [r["_c"] for r in uniq.values() if r["signatory"]["firstName"].strip().title() in names]
        return dec(100 * sum(c == "upper" for c in xs) / len(xs)) if xs else "—"

    s_focus = {r["signatory"]["id"] for r in trows}
    s_big2 = {r["signatory"]["id"] for r in S[big2]}
    s_other = set().union(*[{r["signatory"]["id"] for r in S[pid]} for pid in S if pid != a.focus]) if len(S) > 1 else set()

    first_day = min(kyiv(r["signedAt"]) for r in trows) if trows else now
    elapsed = max(1e-6, (now - first_day).total_seconds() / 86400)
    end = dt.datetime.fromisoformat(t["endedAt"].replace("Z", "+00:00")).astimezone(KYIV)
    days_left = max(1, (end.date() - now.date()).days); left = max(0, 25000 - t["n"])
    na = sum((r["signatory"].get("patronymic") or "").strip().lower() == "n/a" for rows in S.values() for r in rows)
    names = lambda g: "".join(f'<li><span>{e(n)}</span><b class="num">{c}</b></li>' for n, c in collections.Counter(
        r["signatory"]["firstName"].strip().title() for r in trows if r["_g"] == g).most_common(6))
    top2 = sum(p["n"] for p in pets[:2])

    rep = dict(
        ROWS=rows_html, DAYS=dsvg, HOURS=hsvg, GBARS=gbars, CAPSBARS=capsbars,
        NOW=now.strftime("%d.%m.%Y, %H:%M"), NOWT=now.strftime("%H:%M"), LASTDAY=f"{last[8:10]}.{last[5:7]}",
        NPET=str(len(pets)), TOT=fmt(tot), UNIQ=fmt(len(uniq)), FALL=pct(totF, tot, 0), UALL=pct(totU, tot, 0),
        TOP2=pct(top2, tot, 0), SMALLCAPS=smallcaps,
        T_ID=str(a.focus), T_CODE=e(t["code"]), T_TITLE=e(t["title"].strip()),
        T_START=dmy(t["startedAt"]), T_END=dmy(t["endedAt"]),
        T_N=fmt(t["n"]), T_F=fmt(tF), T_M=fmt(tM), T_FP=pct(tF, t["n"]), T_MP=pct(tM, t["n"]),
        T_U=str(t["upper"]), T_UP=pct(t["upper"], t["n"]),
        B_U=dec(bU), B_F=dec(bF), B_M=dec(100 - bF), B_N=fmt(B["total"]), B_K=str(len(base)),
        B_SINCE=dmy(a.baseline_since + "T"),
        B_RANGE=(f'{dec(min(B["sizes"])/1000)}–{dec(max(B["sizes"])/1000)}' if B["sizes"] else "—"),
        S_F=str(sF), S_M=str(sM), S_N=fmt(sN), S_NP=pct(sN, sn), S_DP=pct(sF + sM, sn, 0), S_FP=pct(sF, sF + sM),
        S_WF=f"{100*sF/sn:.2f}", S_WM=f"{100*sM/sn:.2f}", S_WN=f"{100*sN/sn:.2f}", S_OK=str(ok),
        CAPS_F=str(capsF), CAPS_M=str(capsM), CAPS_FP=pct(capsF, tF), CAPS_MP=pct(capsM, tM),
        CAPS_FW=f"{100*capsF/max(1,tF)/30*100:.1f}", CAPS_MW=f"{100*capsM/max(1,tM)/30*100:.1f}",
        PROG=f"{min(100,100*t['n']/25000):.2f}", PROGP=pct(t["n"], 25000), LEFT=fmt(left), DLEFT=str(days_left),
        PACE=f"{left/days_left:.0f}", AVG=f"{t['n']/elapsed:.0f}",
        FEM=names("F"), MAL=names("M"), NA=str(na),
        OVL=str(len(s_focus & s_big2)), BIG2=str(big2), OVL_ANY=str(len(s_focus & s_other)),
        QRANGE=f"{dec(min(qv))}–{dec(max(qv))}", AGE_OLD=share(OLD_NAMES), AGE_YOUNG=share(YOUNG_NAMES),
    )
    out = open(tpl_path, encoding="utf-8").read()
    for k, v in rep.items(): out = out.replace("{{" + k + "}}", v)
    left_ph = re.findall(r"\{\{\w+\}\}", out)
    if left_ph: sys.exit(f"Незаповнені плейсхолдери в шаблоні: {sorted(set(left_ph))}")
    with open(a.out, "w", encoding="utf-8") as f: f.write(out)
    with open(os.path.splitext(a.out)[0] + "-stats.json", "w", encoding="utf-8") as f:
        json.dump({k: v for k, v in rep.items() if "<" not in v}, f, ensure_ascii=False, indent=1)
    print(f"HTML: {a.out}")

    if a.pdf:
        br = next((shutil.which(b) for b in ("chromium", "chromium-browser", "google-chrome-stable", "google-chrome", "brave") if shutil.which(b)), None)
        if not br: sys.exit("PDF: не знайдено chromium/google-chrome")
        pdf = os.path.splitext(a.out)[0] + ".pdf"
        subprocess.run([br, "--headless=new", "--disable-gpu", "--no-pdf-header-footer", f"--print-to-pdf={os.path.abspath(pdf)}",
                        "file://" + os.path.abspath(a.out)], check=True, stderr=subprocess.DEVNULL)
        print(f"PDF:  {pdf}")

if __name__ == "__main__":
    main()
