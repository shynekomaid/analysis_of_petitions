"""Дані для загальносайтового аналізу (kmustat): повний список петицій і підписанти всіх петицій.

Кеш у ``data/kmu/`` стиснутий (gzip) і, як і в petistat, лише доповнюється знімками з часовими
мітками — нічого не перезаписується й не видаляється:

    data/kmu/index/<ts>.json.gz             — усі петиції з content/answer/creator/category/minister
    data/kmu/signers/<id>/<ts>.json.gz      — підписанти петиції у компактному вигляді

Компактний рядок підписанта: ``[signatoryId, firstName, lastName, patronymic, signedAt_epoch_s]``.
Петиції, збір яких завершився, завантажуються лише раз; активні — коли кеш старший за max_age.
"""
from __future__ import annotations

import gzip
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from .api import Client

INCLUDE = "creator,category,minister,organization"


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _parse_ts(stem: str) -> datetime | None:
    try:
        return datetime.strptime(stem.split(".")[0].split("-")[0], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _iso_epoch(s: str | None) -> int:
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()) if s else 0


class KmuStore:
    def __init__(self, cache_dir: str | Path = "data", offline: bool = False, delay: float = 0.15):
        self.root = Path(cache_dir) / "kmu"
        self.offline = offline
        self.client = Client(cache_dir, offline=offline, delay=delay)
        self._lock = threading.Lock()

    # ------------------------------------------------------------ знімки
    def _latest(self, d: Path) -> tuple[Path, datetime] | None:
        if not d.exists():
            return None
        c = [(t, f) for f in d.glob("*.json.gz") if (t := _parse_ts(f.name))]
        if not c:
            return None
        t, f = max(c)
        return f, t

    def _write(self, d: Path, data) -> Path:
        d.mkdir(parents=True, exist_ok=True)
        with self._lock:
            p = d / f"{_ts()}.json.gz"
            n = 1
            while p.exists() or p.with_name(p.name + ".part").exists():
                p = d / f"{_ts()}-{n}.json.gz"; n += 1
            tmp = p.with_name(p.name + ".part")
            tmp.touch()  # резервуємо ім'я, поки пишемо
        with gzip.open(tmp, "wt", encoding="utf-8") as f:  # атомарно: .part → .json.gz
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, p)
        return p

    @staticmethod
    def _read(p: Path):
        with gzip.open(p, "rt", encoding="utf-8") as f:
            return json.load(f)

    # ------------------------------------------------------------ список петицій
    def index(self, max_age_h: float = 24, progress: Callable[[int, int], None] | None = None) -> tuple[list, datetime, bool]:
        d = self.root / "index"
        last = self._latest(d)
        if last and (self.offline or (datetime.now(timezone.utc) - last[1]).total_seconds() < max_age_h * 3600):
            return self._read(last[0]), last[1], True
        if self.offline:
            raise FileNotFoundError("Немає кешу списку петицій (data/kmu/index) — запустіть без --offline")
        rows, start = [], 0
        while True:
            r = self.client._get("petitions", {"start": start, "count": 500, "sortField": "startedAt",
                                               "sortDirection": "desc", "include": INCLUDE})
            rows += r["rows"]; start += len(r["rows"])
            if progress:
                progress(start, r["count"])
            if not r["rows"] or start >= r["count"]:
                break
        self._write(d, rows)
        return rows, datetime.now(timezone.utc), False

    # ------------------------------------------------------------ підписанти
    def _needs_fetch(self, p: dict, max_age_h: float, now: datetime) -> bool:
        last = self._latest(self.root / "signers" / str(p["id"]))
        if self.offline:
            return False
        if not last:
            return True
        end = _iso_epoch(p.get("endedAt"))
        if end and end < now.timestamp() and last[1].timestamp() > end:
            return False  # збір завершено, знімок зроблено після завершення — дані остаточні
        return (now - last[1]).total_seconds() > max_age_h * 3600

    def _fetch_signers(self, pid: int) -> list:
        rows = self.client._get(f"petitions/{pid}/signatories")["rows"]
        return [[r["signatory"]["id"], r["signatory"].get("firstName") or "", r["signatory"].get("lastName") or "",
                 r["signatory"].get("patronymic") or "", _iso_epoch(r["signedAt"])] for r in rows]

    def plan(self, petitions: list, max_age_h: float) -> tuple[list, list]:
        """→ (потрібно завантажити, вже в кеші) серед петицій, що мають підписантів."""
        now = datetime.now(timezone.utc)
        todo, cached = [], []
        for p in petitions:
            (todo if self._needs_fetch(p, max_age_h, now) else cached).append(p)
        return todo, cached

    def download(self, petitions: list, workers: int = 4, on_done: Callable[[dict, int | None], None] | None = None) -> list:
        """Паралельно завантажує підписантів; повертає список помилок (pid, текст)."""
        errors = []

        def job(p):
            rows = self._fetch_signers(p["id"])
            self._write(self.root / "signers" / str(p["id"]), rows)
            return len(rows)

        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            futs = {ex.submit(job, p): p for p in petitions}
            for f in as_completed(futs):
                p = futs[f]
                try:
                    n = f.result()
                except Exception as e:  # мережа — збираємо і продовжуємо
                    errors.append((p["id"], str(e)))
                    n = None
                if on_done:
                    on_done(p, n)
        return errors

    def signers(self, pid: int) -> list | None:
        last = self._latest(self.root / "signers" / str(pid))
        if not last:
            return None
        try:
            return self._read(last[0])
        except (EOFError, OSError, ValueError):  # битий/недописаний знімок — вважаємо, що його немає
            return None

    def iter_signers(self, petitions: list) -> Iterator[tuple[dict, list]]:
        for p in petitions:
            rows = self.signers(p["id"])
            if rows is not None:
                yield p, rows
