"""Точка входу: простий TUI (без аргументів) або неінтерактивний режим (з посиланням)."""
from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.prompt import Confirm, Prompt
from rich.table import Table

from . import __version__
from .analysis import Baseline, baseline_add, build_report, petition_stats, select_baseline
from .api import BASE, Client, PetitionNotFound, parse_petition_ref
from .render import DEFAULT_FONT, FONTS, author_names, find_browser, fmt, pct, write_outputs

console = Console()
STATE = Path("data/state.json")


def _progress() -> Progress:
    return Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), BarColumn(),
                    MofNCompleteColumn(), TimeElapsedColumn(), console=console, transient=True)


# ------------------------------------------------------------------ конвеєр
def run(ref: str, *, cache: str = "data", out: str = "reports", pdf: bool = True, offline: bool = False,
        refresh: bool = False, max_age_h: float = 1.0, baseline_k: int = 12, baseline_since: str = "2026-06-01",
        baseline_max_age_h: float = 24 * 7, template: str | None = None, font: str = DEFAULT_FONT,
        confirm=None) -> dict:
    pid = parse_petition_ref(ref)
    cl = Client(cache, max_age_h=max_age_h, offline=offline)

    with console.status(f"Завантажую петицію {pid}…"):
        try:
            focus_meta = cl.petition(pid, force=refresh).data
        except PetitionNotFound:
            raise SystemExit(f"Петицію {pid} не знайдено на {BASE}")
    creator = focus_meta.get("creator") or {}
    cid = focus_meta.get("creatorId") or creator.get("id")
    full, _, _ = author_names(creator)
    console.print(Panel.fit(f"[bold]{focus_meta.get('title', '').strip()}[/]\n"
                            f"№ {focus_meta.get('code')} · {focus_meta.get('signaturesNumber')} підписів · {focus_meta.get('status')}\n"
                            f"Автор: [bold]{full}[/]" + (f" · {creator['region']['name']}" if creator.get("region") else ""),
                            title=f"Петиція {pid}", border_style="blue"))
    if not cid:
        raise SystemExit("API не повернуло автора цієї петиції (можливо, це петиція організації).")

    with _progress() as pg:
        task = pg.add_task("Список усіх петицій", total=None)
        index_snap = cl.index(force=refresh, progress=lambda d, t: pg.update(task, completed=d, total=t))
    index = index_snap.data
    src = "кеш" if index_snap.from_cache else "сайт"
    mine = sorted([p for p in index if p.get("creatorId") == cid], key=lambda p: -p["signaturesNumber"])
    if pid not in {p["id"] for p in mine}:  # петиція може бути новішою за кеш списку
        mine.append({k: focus_meta.get(k) for k in ("id", "title", "code", "status", "signaturesNumber",
                                                    "creatorId", "startedAt", "createdAt", "endedAt")})
    t = Table(title=f"Петиції автора: {len(mine)} (список: {src}, {index_snap.fetched_at:%d.%m %H:%M} UTC)",
              title_justify="left", header_style="bold", expand=True)
    t.add_column("№", justify="right", no_wrap=True, min_width=5)
    t.add_column("Назва", overflow="fold", ratio=1)
    t.add_column("Підписи", justify="right", no_wrap=True, min_width=7)
    t.add_column("Статус", no_wrap=True, min_width=11)
    for p in mine[:25]:
        t.add_row(str(p["id"]), (p.get("title") or "").strip(), fmt(p.get("signaturesNumber") or 0), p.get("status") or "")
    if len(mine) > 25:
        t.caption = f"…і ще {len(mine) - 25}"
    console.print(t)
    if confirm and not confirm(len(mine)):
        return {}

    stats = []
    with _progress() as pg:
        task = pg.add_task("Підписанти петицій автора", total=len(mine))
        for p in mine:
            pg.update(task, description=f"Підписанти №{p['id']}")
            snap = cl.signatories(p["id"], force=refresh)
            meta = focus_meta if p["id"] == pid else p
            stats.append(petition_stats(meta, snap.data))
            pg.advance(task)

    base = Baseline(since=baseline_since)
    if baseline_k > 0:
        chosen = select_baseline(index, cid, baseline_k, baseline_since)
        with _progress() as pg:
            task = pg.add_task("Еталонна вибірка", total=len(chosen))
            for p in chosen:
                pg.update(task, description=f"Еталон №{p['id']}")
                try:
                    snap = cl.signatories(p["id"], max_age_h=baseline_max_age_h)
                    baseline_add(base, p, snap.data)
                except FileNotFoundError:
                    pass  # offline без кешу для цієї петиції — пропускаємо
                pg.advance(task)

    rep = build_report(creator | {"id": cid}, pid, stats, base)
    with console.status("Рендер HTML" + (" і PDF" if pdf else "") + "…"):
        paths = write_outputs(rep, out, pdf=pdf, template=template, font=font)
    _print_summary(rep, paths, pdf)
    return paths


def _print_summary(rep, paths, want_pdf):
    t, B = rep.focus, rep.baseline
    g = Table(show_header=False, box=None, padding=(0, 2))
    g.add_row("Петицій автора", str(len(rep.petitions)))
    g.add_row("Підписів / унікальних підписантів", f"{fmt(rep.total)} / {fmt(rep.unique)}")
    g.add_row(f"Петиція {t.id}: жінки / чоловіки", f"[#eb6834]{pct(t.F, t.n)}%[/] / [#2a78d6]{pct(t.M, t.n)}%[/]")
    s = rep.surname
    g.add_row("  …визначено за прізвищем", f"{pct(s['F'] + s['M'], max(1, sum(s.values())))}% людей")
    g.add_row(f"Петиція {t.id}: КАПС", f"[#6250d6]{pct(t.upper, t.n)}%[/] ({t.upper})")
    if B.total:
        g.add_row("Еталон: жінки / КАПС", f"{pct(B.F, B.total)}% / {pct(B.upper, B.total)}%  ({fmt(B.total)} підписів)")
    console.print(Panel(g, title="Підсумок", border_style="green", expand=False))
    for k, p in paths.items():
        console.print(f"  [bold]{k.upper():4}[/] {p}")
    if want_pdf and "pdf" not in paths:
        console.print("[yellow]PDF не створено: не знайдено chromium / google-chrome.[/]")


# ------------------------------------------------------------------ TUI
def _load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(d: dict) -> None:
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


FONT_NOTES = {
    "inter": "нейтральний, найкраще читається дрібний текст (рекомендовано)",
    "overpass": "вужчий, «технічний» характер",
    "onest": "м'який гротеск, створений для кирилиці",
    "golos": "щільний і широкий, акцентний",
    "manrope": "геометричний, розріджений",
    "plex": "з характером, трохи «корпоративний»",
}


def _ask_font(default: str) -> str:
    keys = list(FONTS)
    t = Table(show_header=False, box=None, padding=(0, 1))
    for i, k in enumerate(keys, 1):
        mark = "[bold green]•[/]" if k == default else " "
        t.add_row(f"{mark} [bold]{i}[/]", f"[bold]{FONTS[k][0]}[/]", f"[dim]{FONT_NOTES.get(k, '')}[/]")
    console.print("Шрифт звіту:")
    console.print(t)
    choices = [str(i) for i in range(1, len(keys) + 1)] + keys
    ans = Prompt.ask("Номер або назва шрифта", choices=choices, show_choices=False,
                     default=str(keys.index(default) + 1) if default in keys else "1")
    return keys[int(ans) - 1] if ans.isdigit() else ans


def tui(args) -> int:
    console.print(Panel.fit("[bold]petistat[/] — хто підписує петиції до Кабміну\n"
                            "[dim]Вставте посилання на петицію: програма знайде всі петиції її автора\n"
                            "і зробить A4-інфографіку (стать підписантів, КАПС, динаміка).[/]",
                            border_style="blue", subtitle=f"v{__version__}"))
    state = _load_state()
    has_browser = find_browser() is not None
    while True:
        ref = Prompt.ask("Посилання або номер петиції", default=state.get("last") or None)
        if not ref:
            continue
        try:
            parse_petition_ref(ref)
        except ValueError as ex:
            console.print(f"[red]{ex}[/]")
            continue
        refresh = Confirm.ask("Оновити дані з сайту? (ні — використати кеш, якщо він є)", default=True)
        pdf = has_browser and Confirm.ask("Зробити також PDF?", default=True)
        font = _ask_font(state.get("font") or args.font)
        state["last"], state["font"] = ref, font
        _save_state(state)
        try:
            paths = run(ref, cache=args.cache, out=args.out, pdf=pdf, refresh=refresh, offline=False,
                        max_age_h=args.max_age, baseline_k=args.baseline, baseline_since=args.baseline_since,
                        template=args.template, font=font,
                        confirm=lambda n: Confirm.ask(f"Аналізувати {n} {'петицію' if n == 1 else 'петиції' if n < 5 else 'петицій'}?",
                                                      default=True))
        except SystemExit as ex:
            console.print(f"[red]{ex}[/]")
            paths = {}
        except Exception as ex:  # мережа тощо — не вилітаємо з TUI
            console.print(f"[red]Помилка:[/] {ex}")
            paths = {}
        if paths and Confirm.ask("Відкрити звіт у браузері?", default=True):
            webbrowser.open((paths.get("pdf") or paths["html"]).resolve().as_uri())
        if not Confirm.ask("Проаналізувати іншу петицію?", default=False):
            return 0


# ------------------------------------------------------------------ main
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="petistat", description="Інфографіка по підписантах петицій до КМУ.")
    ap.add_argument("petition", nargs="?", help="посилання на петицію або її номер; без нього запускається TUI")
    ap.add_argument("--no-pdf", action="store_true", help="не робити PDF")
    ap.add_argument("--refresh", action="store_true", help="примусово оновити дані автора з сайту")
    ap.add_argument("--offline", action="store_true", help="працювати лише з кешем")
    ap.add_argument("--max-age", type=float, default=1.0, help="вважати кеш свіжим N годин (типово 1)")
    ap.add_argument("--baseline", type=int, default=12, help="кількість петицій для еталону (0 — вимкнути)")
    ap.add_argument("--baseline-since", default="2026-06-01", help="еталон: петиції, що стартували з дати")
    ap.add_argument("--cache", default="data", help="тека кешу (типово data/)")
    ap.add_argument("--out", default="reports", help="тека звітів (типово reports/)")
    ap.add_argument("--template", help="власний HTML-шаблон")
    ap.add_argument("--font", default=DEFAULT_FONT, choices=sorted(FONTS), help=f"шрифт звіту (типово {DEFAULT_FONT})")
    ap.add_argument("--open", action="store_true", help="відкрити звіт після генерації")
    ap.add_argument("-V", "--version", action="version", version=f"petistat {__version__}")
    args = ap.parse_args(argv)
    global STATE
    STATE = Path(args.cache) / "state.json"

    if not args.petition:
        if not sys.stdin.isatty():
            ap.error("вкажіть посилання на петицію (TUI потребує інтерактивного терміналу)")
        try:
            return tui(args)
        except (KeyboardInterrupt, EOFError):
            console.print("\nДо зустрічі!")
            return 130
    try:
        paths = run(args.petition, cache=args.cache, out=args.out, pdf=not args.no_pdf, offline=args.offline,
                    refresh=args.refresh, max_age_h=args.max_age, baseline_k=args.baseline,
                    baseline_since=args.baseline_since, template=args.template, font=args.font)
    except ValueError as ex:
        console.print(f"[red]{ex}[/]")
        return 2
    if args.open and paths:
        webbrowser.open((paths.get("pdf") or paths["html"]).resolve().as_uri())
    return 0
