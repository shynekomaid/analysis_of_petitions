"""Доступ до публічного API petition.kmu.gov.ua з постійним кешем.

Кеш лише доповнюється: кожне завантаження зберігається окремим знімком з часовою
міткою і ніколи не перезаписує та не видаляє попередні. Для читання береться
найсвіжіший знімок.

Структура ``data/``::

    data/
      index/<ts>.json                  — список усіх петицій (легкі поля)
      petitions/<id>/<ts>.json         — повна картка петиції з автором
      signatories/<id>/<ts>.json       — підписанти петиції
      sig_<id>.json, all_petitions.json — кеш першої версії скрипта (лише читання)
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

BASE = "https://petition.kmu.gov.ua"
API = f"{BASE}/api"
UA = {"User-Agent": "Mozilla/5.0 (petistat; +https://petition.kmu.gov.ua)"}
INDEX_FIELDS = "id,title,code,status,signaturesNumber,creatorId,startedAt,createdAt,endedAt"

Progress = Callable[[int, int], None]


class PetitionNotFound(Exception):
    pass


def parse_petition_ref(ref: str) -> int:
    """'https://petition.kmu.gov.ua/petitions/11247', '11247', '41/011247-26еп' → 11247."""
    ref = ref.strip()
    m = re.search(r"/petitions/(\d+)", ref) or re.fullmatch(r"#?(\d+)", ref) or re.fullmatch(r"\d+/0*(\d+)-\S+", ref)
    if not m:
        raise ValueError(f"Не розпізнано посилання або номер петиції: {ref!r}")
    return int(m.group(1))


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


@dataclass
class Snapshot:
    data: object
    path: Path
    fetched_at: datetime
    from_cache: bool


class Client:
    def __init__(self, cache_dir: str | Path = "data", max_age_h: float = 6.0, offline: bool = False,
                 delay: float = 0.3):
        self.root = Path(cache_dir)
        self.max_age_h = max_age_h
        self.offline = offline
        self.delay = delay

    # ------------------------------------------------------------------ HTTP
    def _get(self, path: str, params: dict | None = None, retries: int = 3):
        url = f"{API}/{path}" + ("?" + urllib.parse.urlencode(params) if params else "")
        for i in range(retries):
            try:
                req = urllib.request.Request(url, headers=UA)
                with urllib.request.urlopen(req, timeout=180) as r:
                    data = json.load(r)
                time.sleep(self.delay)
                return data
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    raise PetitionNotFound(path) from e
                if i == retries - 1:
                    raise
            except Exception:
                if i == retries - 1:
                    raise
            time.sleep(2 * (i + 1))

    # ----------------------------------------------------------------- кеш
    def _latest(self, kind: str, key: str | None, legacy: Path | None = None) -> Snapshot | None:
        d = self.root / kind / key if key else self.root / kind
        files = sorted(d.glob("*.json")) if d.exists() else []
        cands: list[tuple[datetime, Path]] = []
        for f in files:
            try:
                cands.append((datetime.strptime(f.stem, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc), f))
            except ValueError:
                pass
        if legacy and legacy.exists():
            cands.append((datetime.fromtimestamp(legacy.stat().st_mtime, timezone.utc), legacy))
        if not cands:
            return None
        when, path = max(cands)
        data = json.loads(path.read_text(encoding="utf-8"))
        return Snapshot(data, path, when, True)

    def _store(self, kind: str, key: str | None, data) -> Snapshot:
        d = self.root / kind / key if key else self.root / kind
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{_ts()}.json"
        n = 1
        while path.exists():  # дві вибірки в одну секунду — не перезаписуємо
            path = d / f"{_ts()}-{n}.json"; n += 1
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return Snapshot(data, path, datetime.now(timezone.utc), False)

    def _cached(self, kind: str, key: str | None, fetch: Callable[[], object], force: bool = False,
                legacy: Path | None = None, max_age_h: float | None = None) -> Snapshot:
        snap = self._latest(kind, key, legacy)
        age_limit = self.max_age_h if max_age_h is None else max_age_h
        if snap and (self.offline or (not force and
                     (datetime.now(timezone.utc) - snap.fetched_at).total_seconds() < age_limit * 3600)):
            return snap
        if self.offline:
            raise FileNotFoundError(f"Немає кешу для {kind}/{key or ''} (режим offline)")
        return self._store(kind, key, fetch())

    # -------------------------------------------------------------- публічне
    def petition(self, pid: int, force: bool = False) -> Snapshot:
        return self._cached("petitions", str(pid),
                            lambda: self._get(f"petitions/{pid}", {"include": "category,creator,organization"}),
                            force=force)

    def index(self, force: bool = False, progress: Progress | None = None) -> Snapshot:
        def fetch():
            rows, start = [], 0
            while True:
                d = self._get("petitions", {"start": start, "count": 500, "sortField": "startedAt",
                                            "sortDirection": "desc", "fields": INDEX_FIELDS})
                rows += d["rows"]; start += len(d["rows"])
                if progress:
                    progress(start, d["count"])
                if not d["rows"] or start >= d["count"]:
                    return rows
        return self._cached("index", None, fetch, force=force, legacy=self.root / "all_petitions.json")

    def signatories(self, pid: int, force: bool = False, max_age_h: float | None = None) -> Snapshot:
        def fetch():
            return self._get(f"petitions/{pid}/signatories")["rows"]
        snap = self._cached("signatories", str(pid), fetch, force=force,
                            legacy=self.root / f"sig_{pid}.json", max_age_h=max_age_h)
        if isinstance(snap.data, dict):  # на випадок, якщо збережено повну відповідь
            snap.data = snap.data.get("rows", [])
        return snap
