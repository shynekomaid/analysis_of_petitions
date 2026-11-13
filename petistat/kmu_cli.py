"""kmustat — загальносайтовий аналіз усіх петицій до КМУ."""
from __future__ import annotations

import argparse
import webbrowser

from rich.console import Console
from rich.panel import Panel
from rich.progress import (BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn,
                           TimeElapsedColumn, TimeRemainingColumn)
from rich.table import Table

from . import __version__
from .kmu_analysis import build
from .kmu_data import KmuStore
from .kmu_render import pc, write_outputs
from .render import DEFAULT_FONT, FONTS, dec, fmt

console = Console()


def _progress() -> Progress:
    return Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), BarColumn(),
                    MofNCompleteColumn(), TimeElapsedColumn(), TextColumn("ETA"), TimeRemainingColumn(),
                    console=console, transient=True)


def _choose_font(cache: str) -> str:
    """Інтерактивний вибір шрифту (лише в терміналі); вибір запам'ятовується в data/state.json."""
    import json
    import sys
    from pathlib import Path
    state_p = Path(cache) / "state.json"
    try:
        state = json.loads(state_p.read_text(encoding="utf-8"))
    except Exception:
        state = {}
    default = state.get("kmustat_font") or DEFAULT_FONT
    if not sys.stdin.isatty():
        return default
    from .cli import _ask_font
    font = _ask_font(default)
    state["kmustat_font"] = font
    try:
        state_p.parent.mkdir(parents=True, exist_ok=True)
        state_p.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return font


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="kmustat", description="Аналіз усіх петицій до Кабміну (petition.kmu.gov.ua).")
    ap.add_argument("--offline", action="store_true", help="лише кеш, без мережі")
    ap.add_argument("--no-signers", action="store_true",
                    help="не завантажувати нових підписантів (аналіз по тому, що вже є в кеші)")
    ap.add_argument("--max-age", type=float, default=24.0, help="свіжість кешу для списку та активних петицій, год")
    ap.add_argument("--workers", type=int, default=4, help="паралельних завантажень (типово 4)")
    ap.add_argument("--cache", default="data")
    ap.add_argument("--out", default="reports")
    ap.add_argument("--font", choices=sorted(FONTS),
                    help=f"шрифт звіту; без опції в терміналі запитає (типово {DEFAULT_FONT})")
    ap.add_argument("--no-pdf", action="store_true")
    ap.add_argument("--no-media", action="store_true", help="не шукати згадки петицій у Google News")
    ap.add_argument("--open", action="store_true", help="відкрити звіт після генерації")
    ap.add_argument("-V", "--version", action="version", version=f"kmustat {__version__}")
    a = ap.parse_args(argv)

    if not a.font:
        a.font = _choose_font(a.cache)
    console.print(Panel.fit("[bold]kmustat[/] — усі петиції до Кабміну: статуси, відповіді, строки, автори, підписанти",
                            border_style="blue", subtitle=f"v{__version__}"))
    store = KmuStore(a.cache, offline=a.offline)

    with _progress() as pg:
        t = pg.add_task("Список петицій", total=None)
        index, index_at, cached = store.index(a.max_age, progress=lambda d, n: pg.update(t, completed=d, total=n))
    console.print(f"Петицій: [bold]{fmt(len(index))}[/] (список: {'кеш' if cached else 'сайт'}, {index_at:%d.%m %H:%M} UTC)")

    with_signers = [p for p in index if p.get("creatorId") and (p.get("signaturesNumber") or 0) > 0]
    todo, have = store.plan(with_signers, a.max_age)
    if a.no_signers or a.offline:
        todo = []
    console.print(f"Петицій з підписантами: {fmt(len(with_signers))} · у кеші: {fmt(len(have))} · завантажити: {fmt(len(todo))}")
    if todo:
        if len(todo) > 300:
            console.print("[dim]Перше завантаження велике (~3,5 млн підписів), це займе до години. "
                          "Перерване завантаження можна продовжити: готові петиції лишаються в кеші.[/]")
        todo.sort(key=lambda p: -(p.get("signaturesNumber") or 0))
        with _progress() as pg:
            t = pg.add_task("Підписанти", total=len(todo))
            errors = store.download(todo, workers=a.workers, on_done=lambda p, n: pg.advance(t))
        if errors:
            console.print(f"[yellow]Не вдалося завантажити {len(errors)} петицій (буде пропущено):[/] "
                          + ", ".join(str(pid) for pid, _ in errors[:10]))

    with _progress() as pg:
        t = pg.add_task("Аналіз підписантів", total=len(with_signers))
        rep = build(index, store.iter_signers(with_signers), index_at, on_progress=lambda n: pg.update(t, completed=n))

    if not a.no_media:
        from .kmu_media import build_idf, fetch_media, media_report, select_targets
        targets = select_targets(rep)
        idf = build_idf(index)
        with _progress() as pg:
            t = pg.add_task("Медіа (Google News)", total=len(targets))
            media = fetch_media(store, targets, idf, progress=lambda i: pg.update(t, completed=i))
        sig_ts = {p["id"]: sorted(r[4] for r in (store.signers(p["id"]) or [])) for p in targets}
        rep.media = media_report(targets, media, sig_ts, idf)

    with console.status("Рендер HTML" + ("" if a.no_pdf else " і PDF") + "…"):
        paths = write_outputs(rep, a.out, pdf=not a.no_pdf, font=a.font)

    g = Table(show_header=False, box=None, padding=(0, 2))
    g.add_row("Петицій / підписів", f"{fmt(rep.n)} / {fmt(rep.total_sig)}")
    g.add_row("Набрали 25 000", f"{len(rep.passed)} ({pc(len(rep.passed), rep.n)}%)")
    g.add_row("Відповідей", f"{len(rep.answered)}")
    if rep.answer_delays:
        import statistics
        g.add_row("Медіана: 25 000 → відповідь", f"{dec(statistics.median(rep.answer_delays), 0)} дн.")
    g.add_row("Автори: жінки", f"{pc(rep.authors_F, rep.authors_F + rep.authors_M)}%")
    g.add_row("Підписи: жінки", f"[#eb6834]{pc(rep.sF, rep.sF + rep.sM)}%[/] ({dec(rep.sig_rows / 1e6, 2)} млн)")
    g.add_row("Унікальних підписантів", fmt(rep.unique_signers))
    if rep.missing_signers:
        g.add_row("[yellow]Без даних про підписантів[/]", f"{rep.missing_signers} петицій")
    console.print(Panel(g, title="Підсумок", border_style="green", expand=False))
    console.print(f"  [bold]PDF [/] {paths.get('pdf_named') or '—'}")
    console.print(f"  [bold]HTML[/] {paths.get('html_named') or paths['html']}")
    console.print(f"  [dim]архів: {paths['html'].parent}/{paths['html'].stem}.*[/]")
    if a.open:
        webbrowser.open((paths.get("pdf_named") or paths.get("pdf") or paths["html"]).resolve().as_uri())
    return 0
