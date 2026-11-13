"""Аналіз текстів: заголовки й тексти петицій (ознаки та успіх), частотні слова, класифікація відповідей уряду."""
from __future__ import annotations

import collections
import re

from .analysis import THRESHOLD
from .kmu_analysis import plain

WORD_RE = re.compile(r"[а-яіїєґ'’]{4,}", re.I)
EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF☀-➿]")
STOP = set("""щодо україни україні україна україну про для також цього цієї яких якого якій який яка які
що щоб його їхніх їхня їхнього наших нашої наші своїх свого своєї усіх всіх всім всіма
шляхом метою разом після перед через між буде будуть можуть може має мають було були
кабінету кабінет міністрів міністерства уряду урядом прошу просимо вимагаємо
питання змін зміни внесення внести внесенню постанови постанову законодавства закону
україні""".split())


def is_passed(p) -> bool:
    return (p.get("signaturesNumber") or 0) >= THRESHOLD or p.get("status") == "Supported"


def caps_ratio(s: str) -> float:
    letters = [c for c in s if c.isalpha()]
    return sum(c.isupper() for c in letters) / len(letters) if letters else 0.0


def title_features(index: list) -> list[tuple[str, list[tuple[str, int, int]]]]:
    """→ [(ознака, [(значення, петицій, набрали 25 000)])]; лише завершені петиції (не Approved)."""
    fin = [p for p in index if p.get("status") != "Approved"]

    def group(name, fn, order):
        c = collections.defaultdict(lambda: [0, 0])
        for p in fin:
            k = fn(p)
            c[k][0] += 1; c[k][1] += is_passed(p)
        return name, [(k, c[k][0], c[k][1]) for k in order if c[k][0]]

    def tlen(p):
        n = len(plain(p.get("title")))
        return "до 60" if n < 60 else "60–119" if n < 120 else "120–199" if n < 200 else "200+"

    def clen(p):
        n = len(plain(p.get("content")))
        return "до 1 тис." if n < 1000 else "1–3 тис." if n < 3000 else "3–6 тис." if n < 6000 else "6 тис.+"

    return [
        group("Заголовок КАПСОМ", lambda p: "так" if caps_ratio(plain(p.get("title"))) >= 0.6 else "ні", ["ні", "так"]),
        group("Знак оклику в заголовку", lambda p: "так" if "!" in (p.get("title") or "") else "ні", ["ні", "так"]),
        group("Емодзі в заголовку", lambda p: "так" if EMOJI_RE.search(p.get("title") or "") else "ні", ["ні", "так"]),
        group("Довжина заголовка, символів", tlen, ["до 60", "60–119", "120–199", "200+"]),
        group("Довжина тексту, символів", clen, ["до 1 тис.", "1–3 тис.", "3–6 тис.", "6 тис.+"]),
    ]


def top_words(index: list, k: int = 14) -> tuple[list, list]:
    """→ (слова успішних петицій з «ліфтом», найчастіші слова всіх петицій). Частота — у скількох заголовках."""
    df_all, df_pass = collections.Counter(), collections.Counter()
    n_all = n_pass = 0
    for p in index:
        words = {w.lower().replace("’", "'") for w in WORD_RE.findall(plain(p.get("title")))} - STOP
        n_all += 1; df_all.update(words)
        if is_passed(p):
            n_pass += 1; df_pass.update(words)
    passed = [(w, c, (c / n_pass) / (df_all[w] / n_all)) for w, c in df_pass.most_common(60) if c >= 3]
    passed.sort(key=lambda x: (-x[1], -x[2]))
    return passed[:k], df_all.most_common(k)


# ------------------------------------------------------------------ відповіді
# «економічно недоцільним» — юридичне визначення (напр. для пошкодженого майна), не відмова
NEG = re.compile(r"(?<!економічно )недоцільн|не вбачається|не може бути підтриман|не підтриму|"
                 r"(?:є|вважає|вважаємо)\s+передчасн|відсутні\s+підстав", re.I)
POS = re.compile(r"уряд\w*\s+(?:в\s+цілому\s+)?підтриму|підтримує\s+(?:порушен|пропозиц|ініціатив|позиці)|"
                 r"поділяє\s+(?:вашу\s+)?позиці", re.I)
PROM = re.compile(r"доручено|опрацьову|буде\s+(?:додатково\s+)?розглянут|будуть\s+(?:додатково\s+)?розглянут|"
                  r"розробля|розроблено\s+проект|підготовлено\s+проект|буде\s+врахован", re.I)
CLASSES = [("refuse", "Відмова або «недоцільно»", NEG), ("support", "Уряд підтримує / поділяє позицію", POS),
           ("promise", "Обіцянка опрацювати / розробляється", PROM)]
OTHER = ("explain", "Лише роз'яснення чинних норм", None)


def sentence_with(text: str, rx: re.Pattern, width: int = 220) -> str:
    """Уривок навколо першого збігу (щоб ключова фраза точно потрапила в цитату)."""
    m = rx.search(text)
    if not m:
        return ""
    a = max(0, m.start() - width // 2)
    b = min(len(text), a + width)
    a = max(0, b - width)
    frag = text[a:b].strip()
    if a > 0:
        frag = "…" + frag.split(" ", 1)[-1]
    if b < len(text):
        frag = frag.rsplit(" ", 1)[0] + "…"
    return frag


def classify_answers(index: list) -> list[tuple[str, str, list[tuple[dict, str]]]]:
    """→ [(код, назва класу, [(петиція, цитата)])] — евристика за ключовими фразами, порядок: відмова > підтримка > обіцянка."""
    buckets = {c[0]: [] for c in CLASSES + [OTHER]}
    for p in index:
        if p.get("status") != "Answered" or not (p.get("answer") or "").strip():
            continue
        t = re.sub(r"\s+", " ", plain(p["answer"]))
        for code, _, rx in CLASSES:
            if rx.search(t):
                buckets[code].append((p, sentence_with(t, rx)))
                break
        else:
            last = re.split(r"(?<=[.!?])\s+", t.strip())
            buckets["explain"].append((p, (last[-1] if last else "")[:220]))
    return [(code, name, buckets[code]) for code, name, _ in CLASSES + [OTHER]]


# ------------------------------------------------------------------ «не та адреса»: компетенція Президента
# Присвоєння/позбавлення звань і державних нагород конкретним особам та помилування вирішує Президент
# (Закон «Про державні нагороди України», ст. 106 Конституції). Запровадження нових нагород (закон) і виплати
# нагородженим (бюджет) — легітимні теми для Кабміну, тому виключаються.
_ACT = r"присво\w*|надат\w*|надан\w*|нагород\w*|позбав\w*|посмертно|відзнач\w*"
WRONG_TYPES = [
    ("hero", "Звання Героя України", re.compile(r"геро[йяюї]\w*(?:\s+україни|\s+посмертно)|звання\s+геро\w*|"
                                                 r"посмертно\s+геро\w*|^\s*посмертно\s*$", re.I)),
    ("honor", "Почесні звання (народний, заслужений…)", re.compile(
        r"почесн\w+\s+званн\w*|народн\w+\s+артист\w*|заслужен\w+\s+(?:артист|діяч|захисник|лікар|вчител|працівник|юрист|майст)\w*", re.I)),
    ("award", "Ордени, медалі, державні нагороди", re.compile(r"\bорден\w*|\bмедал\w*|державн\w+\s+нагород\w*", re.I)),
    ("pardon", "Помилування", re.compile(r"помилув\w*", re.I)),
]
_ACT_RX = re.compile(_ACT, re.I)
_EXCL_RX = re.compile(r"запровадж|засну|встанов\w*\s+(?:нов|державн)|виплат|заохочен|пільг|доплат|грошов|"
                      r"виготов|бланк|купюр|банкнот|гривен|пам['’]ятн|вулиц|перейменув|меморіал", re.I)


def wrong_address(index: list) -> list[tuple[dict, str, str]]:
    """→ [(петиція, код типу, назва типу)] — петиції, рішення за якими ухвалює Президент, а не Кабмін."""
    out = []
    for p in index:
        t = plain(p.get("title"))
        if _EXCL_RX.search(t):
            continue
        for code, name, rx in WRONG_TYPES:
            if rx.search(t) and (code in ("hero", "pardon") or _ACT_RX.search(t)):
                out.append((p, code, name))
                break
    return out


# ------------------------------------------------------------------ 4. час публікації
def publication_time(index: list, early: dict) -> dict:
    """early: pid → підписів за першу добу. Лише завершені петиції нової платформи."""
    from .analysis import KYIV
    from .kmu_analysis import parse_dt
    fin = [p for p in index if p.get("creatorId") and p.get("status") != "Approved" and p.get("startedAt")]
    hb = [(0, 6, "00–06"), (6, 9, "06–09"), (9, 12, "09–12"), (12, 15, "12–15"), (15, 18, "15–18"), (18, 21, "18–21"), (21, 24, "21–24")]
    hours = collections.defaultdict(lambda: [0, 0, []]); wds = collections.defaultdict(lambda: [0, 0, []])
    for p in fin:
        dt = parse_dt(p["startedAt"]).astimezone(KYIV)
        lab = next(l for a, b, l in hb if a <= dt.hour < b)
        for d, k in ((hours, lab), (wds, dt.weekday())):
            d[k][0] += 1; d[k][1] += is_passed(p)
            if p["id"] in early:
                d[k][2].append(early[p["id"]])
    import statistics as st
    return {"hours": [(l, hours[l][0], hours[l][1], st.median(hours[l][2]) if hours[l][2] else 0) for _, _, l in hb],
            "weekdays": [(i, wds[i][0], wds[i][1], st.median(wds[i][2]) if wds[i][2] else 0) for i in range(7)]}


# ------------------------------------------------------------------ 5. мова заголовка
RU_RX = re.compile(r"[ыэъё]|\b(?:что|для\s+всех|или|это|нужно|также|которые|граждан)\b", re.I)
UA_RX = re.compile(r"[іїєґ]", re.I)


def title_language(index: list) -> dict:
    by = collections.defaultdict(lambda: [0, 0, []])
    years = collections.defaultdict(collections.Counter)
    examples = collections.defaultdict(list)
    for p in index:
        t = plain(p.get("title"))
        letters = [c for c in t if c.isalpha()]
        lat = sum(1 for c in letters if "a" <= c.lower() <= "z")
        if letters and lat / len(letters) > 0.5:
            lang = "en"
        elif RU_RX.search(t) and not UA_RX.search(t):
            lang = "ru"
        elif RU_RX.search(t):
            lang = "mix"
        else:
            lang = "uk"
        by[lang][0] += 1; by[lang][1] += is_passed(p); by[lang][2].append(p.get("signaturesNumber") or 0)
        years[(p.get("startedAt") or "????")[:4]][lang] += 1
        if lang != "uk" and len(examples[lang]) < 3:
            examples[lang].append(p)
    import statistics as st
    return {"by": {k: (v[0], v[1], st.median(v[2])) for k, v in by.items()},
            "years": {y: dict(c) for y, c in sorted(years.items())}, "examples": dict(examples)}


# ------------------------------------------------------------------ 6. повторні вимоги після відповіді
def repeat_after_answer(index: list, sim: float = 0.5) -> list:
    """→ [(відповідана петиція, [пізніші схожі петиції])], відсортовано за кількістю повторів."""
    W = re.compile(r"[а-яіїєґa-z0-9'’]{3,}", re.I)
    wset = lambda p: frozenset(w.lower() for w in W.findall(plain(p.get("title")))) - STOP
    ans = [p for p in index if p.get("status") == "Answered" and p.get("answeredAt")]
    later_all = [(p, wset(p)) for p in index if p.get("startedAt")]
    out = []
    for a in ans:
        wa = wset(a)
        if len(wa) < 2:
            continue
        foll = [p for p, wp in later_all if p["id"] != a["id"] and (p.get("startedAt") or "") > a["answeredAt"]
                and wp and len(wa & wp) / len(wa | wp) >= sim]
        if foll:
            out.append((a, sorted(foll, key=lambda p: p.get("startedAt") or "")))
    out.sort(key=lambda x: -len(x[1]))
    return out


# ------------------------------------------------------------------ 7. посилання на законопроєкти / постанови
BILL_RX = re.compile(r"законопро[єе]кт\w*\s*(?:№|n|#)?\s*\d{3,6}|(?:№|n)\s*\d{3,6}(?:-[дd]\b)?|"
                     r"постанов\w*\s+(?:кабінету\s+міністрів|кму)[^.]{0,60}?№\s*\d+", re.I)


def bill_refs(index: list) -> dict:
    fin = [p for p in index if p.get("status") != "Approved"]
    res = {"title": [0, 0], "content": [0, 0], "none": [0, 0]}
    for p in fin:
        if BILL_RX.search(plain(p.get("title"))):
            k = "title"
        elif BILL_RX.search(plain(p.get("content"))):
            k = "content"
        else:
            k = "none"
        res[k][0] += 1; res[k][1] += is_passed(p)
    return {k: tuple(v) for k, v in res.items()}
