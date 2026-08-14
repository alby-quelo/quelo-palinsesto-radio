# -*- coding: utf-8 -*-
"""SQLite clips + anti-overlap per Quelo-palinsesto-radio."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from paths import resolve_db_path

TS_FMT = "%Y-%m-%dT%H:%M:%S"
KIND_FILE = "file"
KIND_LIVE = "live"
KIND_LINK = "link"
KIND_PLAYLIST = "playlist"
LIVE_PATH_DEFAULT = "pulse:default"
DEFAULT_LIVE_COLOR = "#C4783A"
DEFAULT_LINK_COLOR = "#2E8B7A"
DEFAULT_PLAYLIST_COLOR = "#6B5B95"
FILE_PALETTE = (
    "#3D7AB5",
    "#4A9B6E",
    "#8B6BB5",
    "#C45C6A",
    "#3A9B9B",
    "#B8953A",
    "#5C7C9B",
    "#9B5C7C",
)


def default_color_for(kind: str, clip_id: int = 0) -> str:
    if kind == KIND_LIVE:
        return DEFAULT_LIVE_COLOR
    if kind == KIND_LINK:
        return DEFAULT_LINK_COLOR
    if kind == KIND_PLAYLIST:
        return DEFAULT_PLAYLIST_COLOR
    return FILE_PALETTE[max(0, clip_id) % len(FILE_PALETTE)]


def normalize_color(value: str | None, *, kind: str = KIND_FILE, clip_id: int = 0) -> str:
    raw = (value or "").strip()
    if raw.startswith("#") and len(raw) == 7:
        hexpart = raw[1:]
        if all(c in "0123456789abcdefABCDEF" for c in hexpart):
            return f"#{hexpart.upper()}"
    return default_color_for(kind, clip_id)


def normalize_stream_url(url: str) -> str:
    """Valida e normalizza URL http/https per slot LINK."""
    raw = (url or "").strip()
    low = raw.lower()
    if not (low.startswith("http://") or low.startswith("https://")):
        raise ValueError("L'URL deve iniziare con http:// o https://")
    return raw


def link_display_name(url: str) -> str:
    from urllib.parse import urlparse

    try:
        host = (urlparse(url).netloc or "").strip()
        if host:
            return host[:48]
    except Exception:
        pass
    return "LINK"


@dataclass
class Clip:
    id: int
    kind: str
    path: str
    display_name: str  # nome file oppure "LIVE" / host LINK
    title: str  # nome trasmissione (riga 1 timeline)
    description: str
    color: str  # #RRGGBB sfondo blocco
    duration_ms: int
    peak_gain: float
    start_ts: datetime
    end_ts: datetime

    @property
    def is_live(self) -> bool:
        return self.kind == KIND_LIVE

    @property
    def is_link(self) -> bool:
        return self.kind == KIND_LINK

    @property
    def is_playlist(self) -> bool:
        return self.kind == KIND_PLAYLIST

    @property
    def duration(self) -> timedelta:
        return timedelta(milliseconds=self.duration_ms)

    @property
    def source_label(self) -> str:
        """Seconda riga: nome file, LIVE, host LINK o playlist."""
        if self.is_live:
            return "LIVE"
        if self.is_link:
            return self.display_name or "LINK"
        if self.is_playlist:
            return self.display_name or "PLAYLIST"
        return self.display_name or Path(self.path).name

    @property
    def show_title(self) -> str:
        return (self.title or self.display_name or "Senza titolo").strip()

    @property
    def fill_color(self) -> str:
        return normalize_color(self.color, kind=self.kind, clip_id=self.id)


class OverlapError(Exception):
    def __init__(self, other: Clip):
        self.other = other
        super().__init__(
            f"Sovrapposizione con «{other.show_title}» "
            f"({other.start_ts.strftime('%d/%m %H:%M:%S')}–"
            f"{other.end_ts.strftime('%H:%M:%S')})"
        )


def _parse_ts(value: str) -> datetime:
    if "." in value:
        value = value.split(".", 1)[0]
    return datetime.strptime(value, TS_FMT)


def _fmt_ts(dt: datetime) -> str:
    return dt.replace(microsecond=0).strftime(TS_FMT)


def _row_get(row: sqlite3.Row, key: str, default: str = "") -> str:
    keys = row.keys()
    if key not in keys:
        return default
    val = row[key]
    return "" if val is None else str(val)


class PalinsestoDB:
    def __init__(
        self,
        db_path: Path | None = None,
        *,
        check_same_thread: bool = True,
    ) -> None:
        self.path = Path(db_path) if db_path else resolve_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.path), check_same_thread=check_same_thread
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def close(self) -> None:
        self._conn.close()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS clips (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL DEFAULT 'file',
                path TEXT NOT NULL,
                display_name TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                color TEXT NOT NULL DEFAULT '',
                duration_ms INTEGER NOT NULL,
                peak_gain REAL NOT NULL DEFAULT 1.0,
                start_ts TEXT NOT NULL,
                end_ts TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_clips_range
                ON clips(start_ts, end_ts);
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        cols = {
            r[1]
            for r in self._conn.execute("PRAGMA table_info(clips)").fetchall()
        }
        if "kind" not in cols:
            self._conn.execute(
                "ALTER TABLE clips ADD COLUMN kind TEXT NOT NULL DEFAULT 'file'"
            )
        if "title" not in cols:
            self._conn.execute(
                "ALTER TABLE clips ADD COLUMN title TEXT NOT NULL DEFAULT ''"
            )
            # Migra: nome trasmissione = vecchio display_name
            self._conn.execute(
                "UPDATE clips SET title = display_name "
                "WHERE title = '' OR title IS NULL"
            )
        if "description" not in cols:
            self._conn.execute(
                "ALTER TABLE clips ADD COLUMN description TEXT NOT NULL DEFAULT ''"
            )
        if "color" not in cols:
            self._conn.execute(
                "ALTER TABLE clips ADD COLUMN color TEXT NOT NULL DEFAULT ''"
            )
        self._conn.commit()

    def _row_to_clip(self, row: sqlite3.Row) -> Clip:
        kind = _row_get(row, "kind", KIND_FILE) or KIND_FILE
        if kind not in (KIND_FILE, KIND_LIVE, KIND_LINK, KIND_PLAYLIST):
            kind = KIND_FILE
        display_name = _row_get(row, "display_name")
        title = _row_get(row, "title") or display_name
        cid = int(row["id"])
        return Clip(
            id=cid,
            kind=kind,
            path=str(row["path"]),
            display_name=display_name,
            title=title,
            description=_row_get(row, "description"),
            color=normalize_color(
                _row_get(row, "color"), kind=kind, clip_id=cid
            ),
            duration_ms=int(row["duration_ms"]),
            peak_gain=float(row["peak_gain"]),
            start_ts=_parse_ts(str(row["start_ts"])),
            end_ts=_parse_ts(str(row["end_ts"])),
        )

    def list_clips(
        self,
        range_start: datetime | None = None,
        range_end: datetime | None = None,
    ) -> list[Clip]:
        sql = "SELECT * FROM clips"
        params: list[object] = []
        if range_start is not None and range_end is not None:
            sql += " WHERE start_ts < ? AND end_ts > ?"
            params.extend([_fmt_ts(range_end), _fmt_ts(range_start)])
        sql += " ORDER BY start_ts ASC"
        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_clip(r) for r in rows]

    def get_clip(self, clip_id: int) -> Clip | None:
        row = self._conn.execute(
            "SELECT * FROM clips WHERE id = ?", (clip_id,)
        ).fetchone()
        return self._row_to_clip(row) if row else None

    def find_playing(self, when: datetime) -> Clip | None:
        ts = _fmt_ts(when)
        row = self._conn.execute(
            "SELECT * FROM clips WHERE start_ts <= ? AND end_ts > ? "
            "ORDER BY start_ts LIMIT 1",
            (ts, ts),
        ).fetchone()
        return self._row_to_clip(row) if row else None

    def find_overlapping(
        self,
        start: datetime,
        end: datetime,
        exclude_id: int | None = None,
    ) -> Clip | None:
        sql = "SELECT * FROM clips WHERE start_ts < ? AND end_ts > ?"
        params: list[object] = [_fmt_ts(end), _fmt_ts(start)]
        if exclude_id is not None:
            sql += " AND id != ?"
            params.append(exclude_id)
        sql += " ORDER BY start_ts LIMIT 1"
        row = self._conn.execute(sql, params).fetchone()
        return self._row_to_clip(row) if row else None

    def last_clip_ending_on_day(self, day: datetime) -> Clip | None:
        day0 = day.replace(hour=0, minute=0, second=0, microsecond=0)
        day1 = day0 + timedelta(days=1)
        row = self._conn.execute(
            "SELECT * FROM clips WHERE start_ts >= ? AND start_ts < ? "
            "ORDER BY end_ts DESC LIMIT 1",
            (_fmt_ts(day0), _fmt_ts(day1)),
        ).fetchone()
        return self._row_to_clip(row) if row else None

    def last_clip_by_end(self) -> Clip | None:
        """Ultima clip nel DB ordinata per fine (per «In coda»)."""
        row = self._conn.execute(
            "SELECT * FROM clips ORDER BY end_ts DESC LIMIT 1"
        ).fetchone()
        return self._row_to_clip(row) if row else None

    def _insert(
        self,
        *,
        kind: str,
        path: str,
        display_name: str,
        title: str,
        description: str,
        color: str,
        duration_ms: int,
        peak_gain: float,
        start: datetime,
        end: datetime,
    ) -> Clip:
        start = start.replace(microsecond=0)
        end = end.replace(microsecond=0)
        if end <= start:
            raise ValueError("L'orario di fine deve essere dopo l'inizio")
        other = self.find_overlapping(start, end)
        if other is not None:
            raise OverlapError(other)
        title = (title or display_name or "Senza titolo").strip()
        description = (description or "").strip()
        # id non ancora noto: colore temporaneo, poi ritocco dopo insert se vuoto
        color = (color or "").strip()
        cur = self._conn.execute(
            """
            INSERT INTO clips
                (kind, path, display_name, title, description, color,
                 duration_ms, peak_gain, start_ts, end_ts)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                kind,
                path,
                display_name,
                title,
                description,
                color,
                int(duration_ms),
                float(peak_gain),
                _fmt_ts(start),
                _fmt_ts(end),
            ),
        )
        self._conn.commit()
        clip_id = int(cur.lastrowid)
        if not color:
            auto = default_color_for(kind, clip_id)
            self._conn.execute(
                "UPDATE clips SET color = ? WHERE id = ?", (auto, clip_id)
            )
            self._conn.commit()
        clip = self.get_clip(clip_id)
        assert clip is not None
        return clip

    def add_clip(
        self,
        path: Path,
        display_name: str,
        duration_ms: int,
        peak_gain: float,
        start: datetime,
        title: str = "",
        description: str = "",
        color: str = "",
    ) -> Clip:
        start = start.replace(microsecond=0)
        end = start + timedelta(milliseconds=duration_ms)
        return self._insert(
            kind=KIND_FILE,
            path=str(path),
            display_name=display_name or path.name,
            title=title or display_name or path.name,
            description=description,
            color=color,
            duration_ms=duration_ms,
            peak_gain=peak_gain,
            start=start,
            end=end,
        )

    def add_live(
        self,
        start: datetime,
        end: datetime,
        title: str = "Trasmissione live",
        description: str = "",
        device: str = LIVE_PATH_DEFAULT,
        peak_gain: float = 1.0,
        color: str = "",
    ) -> Clip:
        start = start.replace(microsecond=0)
        end = end.replace(microsecond=0)
        duration_ms = int((end - start).total_seconds() * 1000)
        if duration_ms < 1000:
            raise ValueError("Lo slot live deve durare almeno 1 secondo")
        return self._insert(
            kind=KIND_LIVE,
            path=device or LIVE_PATH_DEFAULT,
            display_name="LIVE",
            title=title or "Trasmissione live",
            description=description,
            color=color or DEFAULT_LIVE_COLOR,
            duration_ms=duration_ms,
            peak_gain=peak_gain,
            start=start,
            end=end,
        )

    def add_link(
        self,
        start: datetime,
        end: datetime,
        url: str,
        title: str = "Stream",
        description: str = "",
        peak_gain: float = 1.0,
        color: str = "",
    ) -> Clip:
        start = start.replace(microsecond=0)
        end = end.replace(microsecond=0)
        url = normalize_stream_url(url)
        duration_ms = int((end - start).total_seconds() * 1000)
        if duration_ms < 1000:
            raise ValueError("Lo slot LINK deve durare almeno 1 secondo")
        host = link_display_name(url)
        return self._insert(
            kind=KIND_LINK,
            path=url,
            display_name=host,
            title=title or host or "Stream",
            description=description,
            color=color or DEFAULT_LINK_COLOR,
            duration_ms=duration_ms,
            peak_gain=peak_gain,
            start=start,
            end=end,
        )

    def add_playlist(
        self,
        path: Path,
        display_name: str,
        duration_ms: int,
        peak_gain: float,
        start: datetime,
        title: str = "",
        description: str = "",
        color: str = "",
    ) -> Clip:
        start = start.replace(microsecond=0)
        end = start + timedelta(milliseconds=duration_ms)
        return self._insert(
            kind=KIND_PLAYLIST,
            path=str(path),
            display_name=display_name or path.name,
            title=title or display_name or path.name,
            description=description,
            color=color or DEFAULT_PLAYLIST_COLOR,
            duration_ms=duration_ms,
            peak_gain=peak_gain,
            start=start,
            end=end,
        )

    def update_meta(
        self,
        clip_id: int,
        *,
        title: str,
        description: str,
        color: str | None = None,
        device: str | None = None,
    ) -> Clip:
        """Compat: aggiorna solo etichette/colore/(device LIVE)."""
        return self.update_clip(
            clip_id,
            title=title,
            description=description,
            color=color,
            device=device,
        )

    def update_clip(
        self,
        clip_id: int,
        *,
        title: str | None = None,
        description: str | None = None,
        color: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        device: str | None = None,
        path: str | None = None,
        display_name: str | None = None,
        duration_ms: int | None = None,
        peak_gain: float | None = None,
    ) -> Clip:
        """Aggiornamento completo clip (orari, file/ingresso, meta) con anti-overlap."""
        clip = self.get_clip(clip_id)
        if clip is None:
            raise KeyError(clip_id)

        title_val = (title if title is not None else clip.show_title).strip()
        desc_val = (
            description if description is not None else (clip.description or "")
        ).strip()
        color_val = (
            normalize_color(color, kind=clip.kind, clip_id=clip_id)
            if color is not None
            else clip.fill_color
        )
        new_start = (start or clip.start_ts).replace(microsecond=0)
        new_path = clip.path
        new_display = clip.display_name
        new_duration = clip.duration_ms
        new_peak = clip.peak_gain

        if clip.is_live:
            if device is not None:
                new_path = (
                    device if device.startswith("pulse:") else f"pulse:{device}"
                )
            new_end = (end or clip.end_ts).replace(microsecond=0)
            if new_end <= new_start:
                new_end = new_end + timedelta(days=1)
            new_duration = int((new_end - new_start).total_seconds() * 1000)
            if new_duration < 1000:
                raise ValueError("Lo slot live deve durare almeno 1 secondo")
            new_display = "LIVE"
        elif clip.is_link:
            if path is not None:
                new_path = normalize_stream_url(str(path))
                new_display = (
                    display_name
                    if display_name is not None
                    else link_display_name(new_path)
                )
            elif display_name is not None:
                new_display = display_name
            if peak_gain is not None:
                new_peak = float(peak_gain)
            new_end = (end or clip.end_ts).replace(microsecond=0)
            if new_end <= new_start:
                new_end = new_end + timedelta(days=1)
            new_duration = int((new_end - new_start).total_seconds() * 1000)
            if new_duration < 1000:
                raise ValueError("Lo slot LINK deve durare almeno 1 secondo")
        else:
            # file + playlist
            if path is not None:
                new_path = str(path)
            if display_name is not None:
                new_display = display_name
            if duration_ms is not None:
                new_duration = int(duration_ms)
            if peak_gain is not None:
                new_peak = float(peak_gain)
            if end is not None:
                new_end = end.replace(microsecond=0)
                if new_end <= new_start:
                    raise ValueError("L'orario di fine deve essere dopo l'inizio")
                new_duration = int((new_end - new_start).total_seconds() * 1000)
            else:
                new_end = new_start + timedelta(milliseconds=new_duration)

        other = self.find_overlapping(new_start, new_end, exclude_id=clip_id)
        if other is not None:
            raise OverlapError(other)

        self._conn.execute(
            """
            UPDATE clips SET
                path = ?, display_name = ?, title = ?, description = ?, color = ?,
                duration_ms = ?, peak_gain = ?, start_ts = ?, end_ts = ?
            WHERE id = ?
            """,
            (
                new_path,
                new_display,
                title_val or "Senza titolo",
                desc_val,
                color_val,
                int(new_duration),
                float(new_peak),
                _fmt_ts(new_start),
                _fmt_ts(new_end),
                clip_id,
            ),
        )
        self._conn.commit()
        updated = self.get_clip(clip_id)
        assert updated is not None
        return updated

    def move_clip(self, clip_id: int, new_start: datetime) -> Clip:
        clip = self.get_clip(clip_id)
        if clip is None:
            raise KeyError(clip_id)
        new_start = new_start.replace(microsecond=0)
        new_end = new_start + timedelta(milliseconds=clip.duration_ms)
        return self.update_clip(clip_id, start=new_start, end=new_end)

    def delete_clip(self, clip_id: int) -> None:
        self._conn.execute("DELETE FROM clips WHERE id = ?", (clip_id,))
        self._conn.commit()

    # --- Impostazioni app (tutto nel DB: niente salva_sessione) ---

    def get_setting(self, key: str, default: str = "") -> str:
        row = self._conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return default
        return str(row["value"])

    def set_setting(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._conn.commit()

    def get_float(self, key: str, default: float) -> float:
        raw = self.get_setting(key, "")
        if not raw:
            return default
        try:
            return float(raw)
        except ValueError:
            return default

    def set_float(self, key: str, value: float) -> None:
        self.set_setting(key, f"{value:.6g}")
