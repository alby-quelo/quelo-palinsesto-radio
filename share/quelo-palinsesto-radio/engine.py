# -*- coding: utf-8 -*-
"""Motore headless Quelo-palinsesto-radio (scheduler + player + DB).

Usato dalla UI web (--web-only). Il desktop PyQt resta indipendente.
"""

from __future__ import annotations

import math
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from db import (
    DEFAULT_LINK_COLOR,
    DEFAULT_LIVE_COLOR,
    DEFAULT_PLAYLIST_COLOR,
    LIVE_PATH_DEFAULT,
    Clip,
    OverlapError,
    PalinsestoDB,
    link_display_name,
    normalize_stream_url,
)
from mixer import FileLevelTap, SourceLevelTap, StreamLevelTap
from player import AudioPlayer, gstreamer_available, pump_glib_once
from playlist import AUDIO_SUFFIXES, parse_playlist, probe_playlist, track_durations_ms
from probe import probe_audio
from pulse_sources import (
    ensure_source_ready,
    list_pulse_sources,
    set_source_mute,
    set_source_port,
    set_source_volume_pct,
    split_device,
)

ANTI_BIANCO_CLIP_ID = -1
SETTING_ANTI_BIANCO = "anti_bianco_playlist"
LINK_RETRY_SEC = 5.0

SILENCE_THRESH_DB_DEFAULT = -40.0
SILENCE_HOLD_SEC_DEFAULT = 8.0
SILENCE_RECOVER_SEC_DEFAULT = 2.5
SILENCE_HYST_DB = 5.0
SILENCE_GRACE_SEC = 3.0

SILENCE_KIND_FILE = "file"
SILENCE_KIND_LINK = "link"
SILENCE_KIND_LIVE = "live"

SETTING_CLIP_FONT_PT = "aspect_clip_font_pt"
SETTING_ZOOM_PX_HOUR = "aspect_zoom_px_per_hour"
CLIP_FONT_PT_DEFAULT = 9
PX_PER_HOUR_DEFAULT = 100


def monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def silence_setting_keys(kind: str) -> tuple[str, str, str]:
    return (
        f"silence_{kind}_hold_sec",
        f"silence_{kind}_recover_sec",
        f"silence_{kind}_thresh_db",
    )


def load_silence_params(db: PalinsestoDB, kind: str) -> dict[str, float]:
    hold_k, rec_k, thr_k = silence_setting_keys(kind)
    hold = db.get_float(hold_k, SILENCE_HOLD_SEC_DEFAULT)
    recover = db.get_float(rec_k, SILENCE_RECOVER_SEC_DEFAULT)
    thresh = db.get_float(thr_k, SILENCE_THRESH_DB_DEFAULT)
    return {
        "hold_sec": max(0.5, float(hold)),
        "recover_sec": max(0.5, float(recover)),
        "thresh_db": float(thresh),
        "recover_db": float(thresh) + SILENCE_HYST_DB,
    }


def save_silence_params(
    db: PalinsestoDB, kind: str, hold_sec: float, recover_sec: float, thresh_db: float
) -> None:
    hold_k, rec_k, thr_k = silence_setting_keys(kind)
    db.set_float(hold_k, max(0.5, float(hold_sec)))
    db.set_float(rec_k, max(0.5, float(recover_sec)))
    db.set_float(thr_k, float(thresh_db))


def clip_to_dict(clip: Clip) -> dict[str, Any]:
    return {
        "id": clip.id,
        "kind": clip.kind,
        "path": clip.path,
        "display_name": clip.display_name,
        "title": clip.title,
        "description": clip.description,
        "color": clip.fill_color,
        "duration_ms": clip.duration_ms,
        "peak_gain": clip.peak_gain,
        "start_ts": clip.start_ts.isoformat(timespec="seconds"),
        "end_ts": clip.end_ts.isoformat(timespec="seconds"),
        "show_title": clip.show_title,
        "source_label": clip.source_label,
    }


def _rewrite_playlist_for_folder(
    text: str, suffix: str, folder: Path, saved_names: set[str]
) -> str:
    """Riscrive i path della playlist verso i file salvati in folder."""
    folder = folder.resolve()

    def resolve_ref(raw: str) -> str:
        base = Path(raw.strip()).name
        if not base:
            raise ValueError(f"Riferimento non valido nella playlist: {raw!r}")
        target = folder / base
        if base in saved_names or target.is_file():
            return str(target.resolve())
        raise ValueError(
            f"Manca il file referenziato dalla playlist: {base} "
            "(caricalo insieme alla playlist)"
        )

    lines_out: list[str] = []
    if suffix == ".pls":
        for ln in text.splitlines():
            if "=" not in ln:
                lines_out.append(ln)
                continue
            key, val = ln.split("=", 1)
            key_l = key.strip().lower()
            val = val.strip()
            if key_l.startswith("file") and key_l[4:].isdigit() and val:
                low = val.lower()
                if low.startswith("http://") or low.startswith("https://"):
                    lines_out.append(ln)
                else:
                    lines_out.append(f"{key.strip()}={resolve_ref(val)}")
            else:
                lines_out.append(ln)
        return "\n".join(lines_out) + ("\n" if text.endswith("\n") else "")

    for ln in text.splitlines():
        stripped = ln.strip()
        if (
            not stripped
            or stripped.startswith("#")
            or stripped.lower().startswith("http://")
            or stripped.lower().startswith("https://")
        ):
            lines_out.append(ln)
            continue
        lines_out.append(resolve_ref(stripped))
    return "\n".join(lines_out) + "\n"


def _parse_iso(value: str) -> datetime:
    raw = (value or "").strip().replace("Z", "")
    if "T" not in raw and " " in raw:
        raw = raw.replace(" ", "T", 1)
    if "." in raw:
        raw = raw.split(".", 1)[0]
    return datetime.fromisoformat(raw).replace(microsecond=0)


class PalinsestoEngine:
    """Scheduler + player + CRUD, thread-safe per HTTP."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._lock = threading.RLock()
        self.db = PalinsestoDB(db_path, check_same_thread=False)
        self._running = False
        self._player: AudioPlayer | None = None
        self._level_db = -120.0
        self._status = f"DB: {self.db.path}"
        self._playing_id: int | None = None
        self._web_bind = "0.0.0.0"
        self._web_port = 8890

        self._master = max(0.0, min(1.0, self.db.get_float("master_volume", 0.85)))
        self._default_live_device = self.db.get_setting(
            "default_live_device", "pulse:default"
        )
        self._anti_bianco_path = self.db.get_setting(SETTING_ANTI_BIANCO, "").strip()
        self._anti_bianco_resume_ms = 0
        self._silence_cfg: dict[str, dict[str, float]] = {}
        self._reload_silence_settings()

        self._link_failover_clip_id: int | None = None
        self._link_retry_at = 0.0
        self._silence_failover_clip_id: int | None = None
        self._silence_since: float | None = None
        self._silence_recover_since: float | None = None
        self._silence_grace_until = 0.0
        self._silence_monitor_last_db = -120.0
        self._silence_recover_pending = False
        self._silence_tap: SourceLevelTap | StreamLevelTap | FileLevelTap | None = None
        self._silence_pl_tracks: list[Path] = []
        self._silence_pl_durs: list[int] = []
        self._silence_pl_idx = 0
        self._mixer_taps: dict[str, SourceLevelTap] = {}
        self._mixer_levels: dict[str, float] = {}

        if gstreamer_available():
            self._player = AudioPlayer(
                on_level=self._on_level,
                on_eos=self._on_eos,
                on_error=self._on_player_error,
            )
            self._player.set_master_volume(self._master)
        else:
            self._status = "GStreamer non disponibile"

    # --- lifecycle ---

    def set_web_listen(self, bind: str, port: int) -> None:
        with self._lock:
            self._web_bind = bind
            self._web_port = int(port)

    def web_listen(self) -> tuple[str, int]:
        with self._lock:
            return self._web_bind, self._web_port

    def shutdown(self) -> None:
        with self._lock:
            self._running = False
            self._clear_silence_failover()
            self._clear_link_failover()
            self._stop_mixer_taps()
            if self._player:
                self._player.stop()
            self.db.set_float("master_volume", self._master)
            self.db.close()

    def poll(self) -> None:
        """Da chiamare ~50–250 ms in web-only."""
        with self._lock:
            pump_glib_once()
            for tap in list(self._mixer_taps.values()):
                try:
                    tap.poll()
                except Exception:
                    pass
            if self._silence_failover_clip_id is not None and self._silence_tap is not None:
                try:
                    self._silence_tap.poll()
                except Exception:
                    pass
            if self._running:
                if self._silence_recover_pending:
                    self._try_silence_recover()
                self._scheduler_step()

    # --- status ---

    def status(self) -> dict[str, Any]:
        with self._lock:
            now = datetime.now().replace(microsecond=0)
            playing = self.db.find_playing(now)
            mode = None
            clip_id = None
            if self._player is not None:
                mode = self._player.mode
                clip_id = self._player.clip_id
            failover = None
            if self._link_failover_clip_id is not None:
                failover = "link"
            elif self._silence_failover_clip_id is not None:
                failover = "silence"
            anti = clip_id == ANTI_BIANCO_CLIP_ID
            return {
                "running": self._running,
                "level_db": self._level_db,
                "master_volume": self._master,
                "status": self._status,
                "now": now.isoformat(timespec="seconds"),
                "db_path": str(self.db.path),
                "playing_clip_id": None if anti else clip_id,
                "player_clip_id": clip_id,
                "player_mode": mode,
                "anti_bianco": anti,
                "anti_bianco_path": self._anti_bianco_path,
                "failover": failover,
                "scheduled": clip_to_dict(playing) if playing else None,
                "gstreamer": self._player is not None,
                "port": self._web_port,
                "bind": self._web_bind,
                "mixer_levels": dict(self._mixer_levels),
            }

    # --- transport ---

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._player is None:
                raise RuntimeError("GStreamer non disponibile")
            self._running = True
            self._clear_link_failover()
            self._clear_silence_failover()
            self._silence_grace_until = 0.0
            self._status = f"In onda…  |  DB: {self.db.path}"
            self._scheduler_step()
            # Se dopo lo step non c'è nulla, chiarisci (niente fascia / niente ANTI BIANCO)
            if (
                self._running
                and self._player is not None
                and self._player.clip_id is None
                and not self._anti_bianco_path
            ):
                self._status = (
                    "Nessuna fascia in onda adesso — aggiungi un clip che copra "
                    f"l'ora attuale, oppure imposta ANTI BIANCO  |  DB: {self.db.path}"
                )
            return self.status()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            self._running = False
            self._clear_link_failover()
            self._clear_silence_failover()
            if self._player:
                self._player.stop()
            self._playing_id = None
            self._level_db = -120.0
            self._status = f"Stop  |  DB: {self.db.path}"
            return self.status()

    def set_volume(self, value: float) -> dict[str, Any]:
        with self._lock:
            self._master = max(0.0, min(1.0, float(value)))
            if self._player:
                self._player.set_master_volume(self._master)
            self.db.set_float("master_volume", self._master)
            return self.status()

    # --- clips / week ---

    def list_week(self, monday: str | None = None) -> dict[str, Any]:
        with self._lock:
            if monday:
                m = _parse_iso(monday + "T00:00:00" if "T" not in monday else monday).date()
                m = monday_of(m)
            else:
                m = monday_of(date.today())
            start = datetime.combine(m, datetime.min.time())
            end = start + timedelta(days=7)
            clips = self.db.list_clips(start - timedelta(days=1), end + timedelta(days=1))
            return {
                "week_monday": m.isoformat(),
                "clips": [clip_to_dict(c) for c in clips],
            }

    def queue_start(self, day: str | None = None) -> dict[str, Any]:
        """Prossimo inizio libero: dopo l'ultima clip del DB (o del giorno se day=)."""
        with self._lock:
            last: Clip | None
            if day:
                d = _parse_iso(day + "T00:00:00" if "T" not in day else day)
                last = self.db.last_clip_ending_on_day(d)
                if last is not None:
                    start = last.end_ts
                else:
                    start = d.replace(hour=0, minute=0, second=0, microsecond=0)
            else:
                last = self.db.last_clip_by_end()
                if last is not None:
                    start = last.end_ts
                else:
                    start = datetime.now().replace(microsecond=0)
            end = start + timedelta(hours=1)
            return {
                "start_ts": start.isoformat(timespec="seconds"),
                "end_ts": end.isoformat(timespec="seconds"),
                "after_clip_id": last.id if last else None,
            }

    def get_clip(self, clip_id: int) -> dict[str, Any]:
        with self._lock:
            clip = self.db.get_clip(int(clip_id))
            if clip is None:
                raise KeyError(clip_id)
            return clip_to_dict(clip)

    def delete_clip(self, clip_id: int) -> dict[str, Any]:
        with self._lock:
            clip = self.db.get_clip(int(clip_id))
            if clip is None:
                raise KeyError(clip_id)
            if self._player and self._player.clip_id == clip.id:
                self._player.stop()
            self.db.delete_clip(clip.id)
            return {"ok": True}

    def add_file_clip(self, data: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            path = Path(str(data["path"])).expanduser()
            if not path.is_file():
                raise FileNotFoundError(str(path))
            start = _parse_iso(str(data["start_ts"]))
            duration_ms, peak_gain, title, description = probe_audio(path)
            clip = self.db.add_clip(
                path=path,
                display_name=path.name,
                duration_ms=int(duration_ms) or 1000,
                peak_gain=float(peak_gain) or 1.0,
                start=start,
                title=str(data.get("title") or title or path.stem),
                description=str(data.get("description") or description or ""),
                color=str(data.get("color") or ""),
            )
            return clip_to_dict(clip)

    def add_playlist_clip(self, data: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            path = Path(str(data["path"])).expanduser()
            if not path.is_file():
                raise FileNotFoundError(str(path))
            start = _parse_iso(str(data["start_ts"]))
            total, peak, title, description, _tracks, _durs = probe_playlist(path)
            clip = self.db.add_playlist(
                path=path,
                display_name=path.name,
                duration_ms=int(total) or 1000,
                peak_gain=float(peak) or 1.0,
                start=start,
                title=str(data.get("title") or title or path.stem),
                description=str(data.get("description") or description or ""),
                color=str(data.get("color") or DEFAULT_PLAYLIST_COLOR),
            )
            return clip_to_dict(clip)

    def add_live_clip(self, data: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            start = _parse_iso(str(data["start_ts"]))
            end = _parse_iso(str(data["end_ts"]))
            device = str(data.get("device") or self._default_live_device)
            clip = self.db.add_live(
                start=start,
                end=end,
                title=str(data.get("title") or "Trasmissione live"),
                description=str(data.get("description") or ""),
                device=device,
                peak_gain=float(data.get("peak_gain") or 1.0),
                color=str(data.get("color") or DEFAULT_LIVE_COLOR),
            )
            return clip_to_dict(clip)

    def add_link_clip(self, data: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            start = _parse_iso(str(data["start_ts"]))
            end = _parse_iso(str(data["end_ts"]))
            url = normalize_stream_url(str(data["url"]))
            clip = self.db.add_link(
                start=start,
                end=end,
                url=url,
                title=str(data.get("title") or link_display_name(url)),
                description=str(data.get("description") or ""),
                peak_gain=float(data.get("peak_gain") or 1.0),
                color=str(data.get("color") or DEFAULT_LINK_COLOR),
            )
            return clip_to_dict(clip)

    def update_clip(self, clip_id: int, data: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            kwargs: dict[str, Any] = {}
            for key in (
                "title",
                "description",
                "color",
                "device",
                "path",
                "display_name",
                "duration_ms",
                "peak_gain",
            ):
                if key in data and data[key] is not None:
                    kwargs[key] = data[key]
            if "start_ts" in data and data["start_ts"]:
                kwargs["start"] = _parse_iso(str(data["start_ts"]))
            if "end_ts" in data and data["end_ts"]:
                kwargs["end"] = _parse_iso(str(data["end_ts"]))
            clip = self.db.update_clip(int(clip_id), **kwargs)
            return clip_to_dict(clip)

    # --- settings / anti bianco / devices ---

    def get_settings(self) -> dict[str, Any]:
        with self._lock:
            return {
                "master_volume": self._master,
                "default_live_device": self._default_live_device,
                "anti_bianco_playlist": self._anti_bianco_path,
                "aspect_clip_font_pt": int(
                    round(self.db.get_float(SETTING_CLIP_FONT_PT, float(CLIP_FONT_PT_DEFAULT)))
                ),
                "aspect_zoom_px_per_hour": int(
                    round(self.db.get_float(SETTING_ZOOM_PX_HOUR, float(PX_PER_HOUR_DEFAULT)))
                ),
                "silence": {
                    "file": self._silence_cfg[SILENCE_KIND_FILE],
                    "link": self._silence_cfg[SILENCE_KIND_LINK],
                    "live": self._silence_cfg[SILENCE_KIND_LIVE],
                },
            }

    def set_settings(self, data: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if "master_volume" in data:
                self._master = max(0.0, min(1.0, float(data["master_volume"])))
                if self._player:
                    self._player.set_master_volume(self._master)
                self.db.set_float("master_volume", self._master)
            if "default_live_device" in data:
                self._default_live_device = str(data["default_live_device"] or "pulse:default")
                self.db.set_setting("default_live_device", self._default_live_device)
            if "anti_bianco_playlist" in data:
                self._set_anti_bianco(str(data["anti_bianco_playlist"] or "").strip())
            if "aspect_clip_font_pt" in data:
                self.db.set_setting(SETTING_CLIP_FONT_PT, str(int(data["aspect_clip_font_pt"])))
            if "aspect_zoom_px_per_hour" in data:
                self.db.set_setting(SETTING_ZOOM_PX_HOUR, str(int(data["aspect_zoom_px_per_hour"])))
            silence = data.get("silence") or {}
            for kind in (SILENCE_KIND_FILE, SILENCE_KIND_LINK, SILENCE_KIND_LIVE):
                block = silence.get(kind)
                if not isinstance(block, dict):
                    continue
                save_silence_params(
                    self.db,
                    kind,
                    float(block.get("hold_sec", SILENCE_HOLD_SEC_DEFAULT)),
                    float(block.get("recover_sec", SILENCE_RECOVER_SEC_DEFAULT)),
                    float(block.get("thresh_db", SILENCE_THRESH_DB_DEFAULT)),
                )
            self._reload_silence_settings()
            return self.get_settings()

    def _stop_mixer_taps(self) -> None:
        for tap in self._mixer_taps.values():
            try:
                tap.stop()
            except Exception:
                pass
        self._mixer_taps.clear()
        self._mixer_levels.clear()

    def _ensure_mixer_taps(self) -> None:
        """Una tap VU per ingresso Pulse (porta attiva), senza uscita audio."""
        if not gstreamer_available():
            return
        wanted: dict[str, str] = {}
        for src in list_pulse_sources():
            if src.is_monitor:
                continue
            if src.port and not src.port_active:
                continue
            base = src.source_name or split_device(src.name)[0]
            if base and base not in wanted:
                wanted[base] = src.name
        for base in list(self._mixer_taps):
            if base not in wanted:
                try:
                    self._mixer_taps[base].stop()
                except Exception:
                    pass
                self._mixer_taps.pop(base, None)
                self._mixer_levels.pop(base, None)
        for base, name in wanted.items():
            if base in self._mixer_taps:
                continue
            try:
                device = ensure_source_ready(name)

                def _on_db(db: float, key: str = base) -> None:
                    self._mixer_levels[key] = float(db)

                self._mixer_taps[base] = SourceLevelTap(device, on_db=_on_db)
                self._mixer_levels[base] = -120.0
            except Exception:
                self._mixer_levels[base] = -120.0

    def devices(self) -> dict[str, Any]:
        with self._lock:
            self._ensure_mixer_taps()
            sources = []
            for s in list_pulse_sources():
                active = bool(s.is_monitor or not s.port or s.port_active)
                base = s.source_name or split_device(s.name)[0]
                level = float(self._mixer_levels.get(base, -120.0)) if active and not s.is_monitor else -120.0
                sources.append(
                    {
                        "name": s.name,
                        "description": s.description,
                        "label": s.label,
                        "muted": s.muted,
                        "volume_pct": s.volume_pct,
                        "port": s.port,
                        "port_active": s.port_active,
                        "source_name": s.source_name,
                        "is_monitor": s.is_monitor,
                        "level_db": level,
                    }
                )
            return {
                "sources": sources,
                "default_live_device": self._default_live_device,
                "mixer_levels": dict(self._mixer_levels),
            }

    def mixer_set(self, data: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            name = str(data.get("name") or "")
            if not name:
                raise ValueError("name richiesto")
            if "mute" in data:
                set_source_mute(name, bool(data["mute"]))
            if "volume_pct" in data:
                set_source_volume_pct(name, float(data["volume_pct"]))
            if "port" in data and data["port"]:
                set_source_port(name, str(data["port"]))
            return self.devices()

    def browse(self, path_s: str) -> dict[str, Any]:
        with self._lock:
            raw = (path_s or "").strip() or str(Path.home())
            path = Path(raw).expanduser()
            if not path.exists():
                raise FileNotFoundError(str(path))
            if path.is_file():
                return {
                    "path": str(path),
                    "type": "file",
                    "name": path.name,
                    "parent": str(path.parent),
                }
            entries = []
            try:
                children = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
            except PermissionError as exc:
                raise PermissionError(str(exc)) from exc
            for child in children:
                if child.name.startswith("."):
                    continue
                entries.append(
                    {
                        "name": child.name,
                        "path": str(child),
                        "type": "dir" if child.is_dir() else "file",
                    }
                )
            return {
                "path": str(path),
                "type": "dir",
                "parent": str(path.parent),
                "entries": entries,
            }

    def mkdir(self, data: dict[str, Any]) -> dict[str, Any]:
        """Crea una cartella nella posizione attuale del browser file."""
        with self._lock:
            parent = Path(str(data.get("path") or "")).expanduser()
            name = Path(str(data.get("name") or "").strip()).name.strip()
            if not name or name in (".", ".."):
                raise ValueError("Nome cartella non valido")
            if not parent.is_dir():
                raise FileNotFoundError(f"Cartella non trovata: {parent}")
            dest = parent / name
            if dest.exists():
                raise FileExistsError(f"Esiste già: {dest}")
            dest.mkdir(parents=False)
            return {"ok": True, "path": str(dest), "parent": str(parent), "name": name}

    def create_playlist_file(self, data: dict[str, Any]) -> dict[str, Any]:
        """Scrive un .m3u nella cartella indicata, con i file scelti dal browser."""
        with self._lock:
            name = str(data.get("name") or "").strip()
            folder = Path(str(data.get("dir") or "")).expanduser()
            raw_tracks = data.get("tracks") or []
            if not name:
                raise ValueError("Nome playlist mancante")
            safe = Path(name).name.strip()
            if not safe or safe in (".", ".."):
                raise ValueError("Nome playlist non valido")
            if not safe.lower().endswith((".m3u", ".m3u8")):
                safe += ".m3u"
            if not folder.is_dir():
                raise FileNotFoundError(f"Cartella non trovata: {folder}")
            dest = (folder / safe).resolve()
            if dest.exists():
                raise FileExistsError(f"Esiste già: {dest}")
            paths: list[Path] = []
            for raw in raw_tracks:
                p = Path(str(raw)).expanduser()
                if not p.is_file():
                    raise FileNotFoundError(str(p))
                if p.suffix.lower() not in AUDIO_SUFFIXES:
                    raise ValueError(f"Non è un file audio: {p.name}")
                paths.append(p.resolve())
            if not paths:
                raise ValueError("Aggiungi almeno un file con +")
            lines = ["#EXTM3U"] + [str(p) for p in paths] + [""]
            dest.write_text("\n".join(lines), encoding="utf-8")
            return {"ok": True, "path": str(dest), "count": len(paths), "name": dest.name}

    def upload_media(
        self,
        *,
        dest_dir: str,
        mode: str,
        files: list[tuple[str, bytes]],
        playlist_file: tuple[str, bytes] | None = None,
    ) -> dict[str, Any]:
        """Salva file audio e, in mode=playlist, anche il file playlist già pronto."""
        with self._lock:
            folder = Path(str(dest_dir or "")).expanduser()
            if not folder.is_dir():
                raise FileNotFoundError(f"Cartella non trovata: {folder}")
            mode_n = (mode or "file").strip().lower()
            if mode_n not in ("file", "playlist"):
                raise ValueError("mode deve essere file o playlist")
            if mode_n == "file":
                if len(files) != 1:
                    raise ValueError("Per file singolo carica un solo file")
                if playlist_file is not None:
                    raise ValueError("In mode file non serve la playlist")
            if mode_n == "playlist":
                if playlist_file is None:
                    raise ValueError("Seleziona il file playlist (.m3u / .m3u8 / .pls)")
                if not files:
                    raise ValueError("Carica anche i file audio referenziati dalla playlist")

            prepared: list[tuple[Path, bytes]] = []
            for raw_name, content in files:
                safe = Path(str(raw_name or "")).name.strip()
                if not safe or safe in (".", ".."):
                    raise ValueError("Nome file non valido")
                if Path(safe).suffix.lower() not in AUDIO_SUFFIXES:
                    raise ValueError(f"Non è un file audio: {safe}")
                if not content:
                    raise ValueError(f"File vuoto: {safe}")
                target = (folder / safe).resolve()
                try:
                    target.relative_to(folder.resolve())
                except ValueError as exc:
                    raise ValueError(f"Percorso non consentito: {safe}") from exc
                if target.exists():
                    raise FileExistsError(f"Esiste già: {target}")
                prepared.append((target, content))

            pl_target: Path | None = None
            pl_content: bytes | None = None
            if mode_n == "playlist":
                assert playlist_file is not None
                pl_name, pl_content = playlist_file
                safe_pl = Path(str(pl_name or "")).name.strip()
                if not safe_pl or safe_pl in (".", ".."):
                    raise ValueError("Nome playlist non valido")
                if Path(safe_pl).suffix.lower() not in (".m3u", ".m3u8", ".pls"):
                    raise ValueError("La playlist deve essere .m3u / .m3u8 / .pls")
                if not pl_content:
                    raise ValueError("File playlist vuoto")
                pl_target = (folder / safe_pl).resolve()
                try:
                    pl_target.relative_to(folder.resolve())
                except ValueError as exc:
                    raise ValueError(f"Percorso non consentito: {safe_pl}") from exc
                if pl_target.exists():
                    raise FileExistsError(f"Esiste già: {pl_target}")

            saved: list[str] = []
            saved_names: set[str] = set()
            for target, content in prepared:
                target.write_bytes(content)
                saved.append(str(target))
                saved_names.add(target.name)

            out: dict[str, Any] = {
                "ok": True,
                "mode": mode_n,
                "dir": str(folder),
                "files": saved,
                "count": len(saved),
            }
            if mode_n == "file":
                out["path"] = saved[0]
                return out

            assert pl_target is not None and pl_content is not None
            text = pl_content.decode("utf-8", errors="replace")
            rewritten = _rewrite_playlist_for_folder(
                text, pl_target.suffix.lower(), folder, saved_names
            )
            pl_target.write_text(rewritten, encoding="utf-8")
            out["playlist"] = str(pl_target)
            out["name"] = pl_target.name
            return out

    # --- internal player callbacks / scheduler (ported from ui.MainWindow) ---

    def _on_level(self, db: float) -> None:
        with self._lock:
            self._level_db = float(db)
            self._silence_observe_program(float(db))

    def _on_eos(self) -> None:
        with self._lock:
            self._playing_id = None
            if self._running:
                self._maybe_link_failover("stream terminato (EOS)")

    def _on_player_error(self, msg: str) -> None:
        with self._lock:
            self._playing_id = None
            if self._running and self._maybe_link_failover(msg):
                return
            self._status = f"Errore player: {msg}"

    def _set_anti_bianco(self, new_path: str) -> None:
        if new_path:
            pl = Path(new_path)
            if not pl.is_file():
                raise FileNotFoundError(new_path)
            tracks = parse_playlist(pl)
            if not tracks:
                raise ValueError("Playlist vuota o senza file audio locali")
        prev = self._anti_bianco_path
        self._anti_bianco_path = new_path
        self.db.set_setting(SETTING_ANTI_BIANCO, new_path)
        if new_path != prev:
            self._anti_bianco_resume_ms = 0
        if (
            self._running
            and self._player
            and self._player.clip_id == ANTI_BIANCO_CLIP_ID
            and new_path != prev
        ):
            self._player.stop()
            self._playing_id = None
        self._status = (
            f"ANTI BIANCO impostato: {Path(new_path).name}"
            if new_path
            else "ANTI BIANCO disattivato"
        )

    def _reload_silence_settings(self) -> None:
        self._silence_cfg = {
            SILENCE_KIND_FILE: load_silence_params(self.db, SILENCE_KIND_FILE),
            SILENCE_KIND_LINK: load_silence_params(self.db, SILENCE_KIND_LINK),
            SILENCE_KIND_LIVE: load_silence_params(self.db, SILENCE_KIND_LIVE),
        }

    def _clear_link_failover(self) -> None:
        self._link_failover_clip_id = None
        self._link_retry_at = 0.0

    def _stop_silence_tap(self) -> None:
        tap = self._silence_tap
        self._silence_tap = None
        if tap is not None:
            try:
                tap.stop()
            except Exception:
                pass

    def _clear_silence_failover(self) -> None:
        self._stop_silence_tap()
        self._silence_failover_clip_id = None
        self._silence_since = None
        self._silence_recover_since = None
        self._silence_monitor_last_db = -120.0
        self._silence_recover_pending = False
        self._silence_pl_tracks = []
        self._silence_pl_durs = []
        self._silence_pl_idx = 0

    def _silence_kind_for_clip(self, clip: Clip) -> str:
        if clip.is_live:
            return SILENCE_KIND_LIVE
        if clip.is_link:
            return SILENCE_KIND_LINK
        return SILENCE_KIND_FILE

    def _silence_params_for_clip(self, clip: Clip) -> dict[str, float]:
        return self._silence_cfg[self._silence_kind_for_clip(clip)]

    def _silence_params_failover(self) -> dict[str, float] | None:
        if self._silence_failover_clip_id is None:
            return None
        clip = self.db.get_clip(self._silence_failover_clip_id)
        if clip is None:
            return None
        return self._silence_params_for_clip(clip)

    def _program_db_pre_master(self, vu_db: float) -> float:
        m = float(self._master)
        if m <= 0.001:
            return -120.0
        return float(vu_db) - 20.0 * math.log10(m)

    def _silence_observe_program(self, vu_db: float) -> None:
        if not self._running or self._player is None:
            return
        if self._link_failover_clip_id is not None:
            return
        if self._silence_failover_clip_id is not None:
            return
        if self._player.clip_id is None or self._player.clip_id == ANTI_BIANCO_CLIP_ID:
            self._silence_since = None
            return
        if time.monotonic() < self._silence_grace_until:
            self._silence_since = None
            return
        if self._master <= 0.001:
            self._silence_since = None
            return
        clip = self.db.find_playing(datetime.now().replace(microsecond=0))
        if clip is None or clip.id != self._player.clip_id:
            self._silence_since = None
            return
        cfg = self._silence_params_for_clip(clip)
        thresh = cfg["thresh_db"]
        hold = cfg["hold_sec"]
        prog = self._program_db_pre_master(vu_db)
        now_m = time.monotonic()
        if prog < thresh:
            if self._silence_since is None:
                self._silence_since = now_m
            elif now_m - self._silence_since >= hold:
                self._enter_silence_failover(
                    clip, f"audio < {thresh:.0f} dBFS per {hold:.1f}s"
                )
        else:
            self._silence_since = None

    def _enter_silence_failover(self, clip: Clip, reason: str = "") -> None:
        self._clear_link_failover()
        self._silence_failover_clip_id = clip.id
        self._silence_since = None
        self._silence_recover_since = None
        self._silence_recover_pending = False
        detail = f" — {reason}" if reason else ""
        self._status = f"Silenzio → ANTI BIANCO{detail}  |  DB: {self.db.path}"
        self._capture_anti_bianco_resume()
        if self._player is None or self._player.clip_id != ANTI_BIANCO_CLIP_ID:
            self._scheduler_anti_bianco(silence_failover=True)
        self._start_silence_monitor(clip)

    def _on_silence_monitor_db(self, db: float) -> None:
        with self._lock:
            self._silence_monitor_last_db = float(db)
            self._silence_evaluate_recover(float(db))

    def _silence_evaluate_recover(self, db: float) -> None:
        if not self._running or self._silence_failover_clip_id is None:
            return
        if self._silence_recover_pending:
            return
        cfg = self._silence_params_failover()
        if cfg is None:
            return
        recover_db = cfg["recover_db"]
        recover_sec = cfg["recover_sec"]
        now_m = time.monotonic()
        if float(db) >= recover_db:
            if self._silence_recover_since is None:
                self._silence_recover_since = now_m
            elif now_m - self._silence_recover_since >= recover_sec:
                self._silence_recover_pending = True
        else:
            self._silence_recover_since = None

    def _start_silence_monitor(self, clip: Clip) -> None:
        self._stop_silence_tap()
        self._silence_monitor_last_db = -120.0
        self._silence_recover_since = None
        try:
            if clip.is_live:
                raw = (clip.path or LIVE_PATH_DEFAULT).strip()
                if raw.startswith("pulse:"):
                    raw = raw[6:]
                if not raw or raw == "default":
                    device = ensure_source_ready("default")
                    tap_dev = "" if device == "default" else device
                else:
                    tap_dev = ensure_source_ready(raw)
                    if tap_dev == "default":
                        tap_dev = ""
                self._silence_tap = SourceLevelTap(
                    tap_dev,
                    on_db=self._on_silence_monitor_db,
                    on_error=lambda m: None,
                )
                return
            if clip.is_link:
                self._silence_tap = StreamLevelTap(
                    clip.path,
                    on_db=self._on_silence_monitor_db,
                    on_error=lambda m: None,
                )
                return
            if clip.is_playlist:
                pl = Path(clip.path)
                tracks = parse_playlist(pl)
                durs = track_durations_ms(tracks)
                if not tracks:
                    return
                now = datetime.now()
                rem = max(0, int((now - clip.start_ts).total_seconds() * 1000))
                total = sum(durs) or 1
                rem = rem % total
                idx = 0
                while idx < len(durs) and rem >= durs[idx]:
                    rem -= durs[idx]
                    idx += 1
                if idx >= len(tracks):
                    idx = 0
                    rem = 0
                self._silence_pl_tracks = list(tracks)
                self._silence_pl_durs = list(durs)
                self._silence_pl_idx = idx
                self._silence_tap = FileLevelTap(
                    str(tracks[idx]),
                    on_db=self._on_silence_monitor_db,
                    on_eos=self._silence_monitor_playlist_advance,
                    start_offset_ms=rem,
                )
                return
            path = Path(clip.path)
            if not path.is_file():
                return
            now = datetime.now()
            offset = max(0, int((now - clip.start_ts).total_seconds() * 1000))
            offset = min(offset, max(0, clip.duration_ms - 50))
            self._silence_tap = FileLevelTap(
                str(path),
                on_db=self._on_silence_monitor_db,
                start_offset_ms=offset,
            )
        except Exception as exc:  # noqa: BLE001
            self._status = f"Silenzio: monitor fallito ({exc})"

    def _silence_monitor_playlist_advance(self) -> None:
        with self._lock:
            if not self._silence_pl_tracks:
                return
            nxt = (self._silence_pl_idx + 1) % len(self._silence_pl_tracks)
            self._silence_pl_idx = nxt
            self._stop_silence_tap()
            try:
                self._silence_tap = FileLevelTap(
                    str(self._silence_pl_tracks[nxt]),
                    on_db=self._on_silence_monitor_db,
                    on_eos=self._silence_monitor_playlist_advance,
                    start_offset_ms=0,
                )
            except Exception:
                pass

    def _try_silence_recover(self) -> None:
        self._silence_recover_pending = False
        if self._silence_failover_clip_id is None or self._player is None:
            return
        clip_id = self._silence_failover_clip_id
        clip = self.db.get_clip(clip_id)
        now = datetime.now().replace(microsecond=0)
        playing = self.db.find_playing(now)
        if clip is None or playing is None or playing.id != clip_id:
            self._clear_silence_failover()
            return
        self._capture_anti_bianco_resume()
        self._clear_silence_failover()
        self._silence_grace_until = time.monotonic() + SILENCE_GRACE_SEC
        try:
            if self._player.clip_id is not None:
                self._player.stop()
        except Exception:
            pass
        self._status = f"Audio ripreso → {playing.show_title}  |  DB: {self.db.path}"
        self._scheduler_step()

    def _maybe_link_failover(self, reason: str = "") -> bool:
        if not self._running or self._player is None:
            return False
        now = datetime.now().replace(microsecond=0)
        clip = self.db.find_playing(now)
        if clip is None or not clip.is_link:
            return False
        self._enter_link_failover(clip, reason)
        return True

    def _enter_link_failover(self, clip: Clip, reason: str = "") -> None:
        self._clear_silence_failover()
        self._link_failover_clip_id = clip.id
        self._link_retry_at = time.monotonic() + LINK_RETRY_SEC
        detail = f" — {reason}" if reason else ""
        self._status = f"LINK offline → ANTI BIANCO{detail}  |  DB: {self.db.path}"
        if self._player is not None and self._player.clip_id == ANTI_BIANCO_CLIP_ID:
            return
        self._scheduler_anti_bianco(link_failover=True)

    @staticmethod
    def _probe_link_url(url: str, timeout: float = 2.5) -> bool:
        raw = (url or "").strip()
        if not raw:
            return False
        try:
            req = urllib.request.Request(
                raw,
                headers={"User-Agent": "Quelo-palinsesto-radio/link-probe"},
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                chunk = resp.read(2048)
                return bool(chunk)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
            return False
        except Exception:
            return False

    def _capture_anti_bianco_resume(self) -> None:
        if self._player is None:
            return
        if self._player.clip_id != ANTI_BIANCO_CLIP_ID:
            return
        self._anti_bianco_resume_ms = self._player.playlist_position_ms()

    def _try_link_recover(self, clip: Clip) -> bool:
        assert self._player is not None
        self._link_retry_at = time.monotonic() + LINK_RETRY_SEC
        if not self._probe_link_url(clip.path):
            if self._player.clip_id != ANTI_BIANCO_CLIP_ID:
                self._scheduler_anti_bianco(link_failover=True)
            return False
        try:
            self._capture_anti_bianco_resume()
            self._player.play_stream(
                clip.path,
                clip_id=clip.id,
                peak_gain=clip.peak_gain,
            )
        except Exception as exc:  # noqa: BLE001
            self._status = f"LINK ancora offline ({exc}) → ANTI BIANCO"
            self._scheduler_anti_bianco(link_failover=True)
            return False
        self._clear_link_failover()
        self._playing_id = clip.id
        self._status = f"LINK ripreso: {clip.show_title}"
        return True

    def _scheduler_step(self) -> None:
        if not self._running or self._player is None:
            return
        now = datetime.now().replace(microsecond=0)
        clip = self.db.find_playing(now)
        if clip is None:
            self._clear_link_failover()
            self._clear_silence_failover()
            self._scheduler_anti_bianco()
            return

        if (
            self._link_failover_clip_id is not None
            and clip.is_link
            and clip.id == self._link_failover_clip_id
        ):
            if time.monotonic() >= self._link_retry_at:
                self._try_link_recover(clip)
                return
            if self._player.clip_id != ANTI_BIANCO_CLIP_ID:
                self._scheduler_anti_bianco(link_failover=True)
            return

        if (
            self._silence_failover_clip_id is not None
            and clip.id == self._silence_failover_clip_id
        ):
            if self._player.clip_id != ANTI_BIANCO_CLIP_ID:
                self._scheduler_anti_bianco(silence_failover=True)
            if self._silence_tap is None:
                self._start_silence_monitor(clip)
            return

        if self._link_failover_clip_id is not None:
            self._clear_link_failover()
        if (
            self._silence_failover_clip_id is not None
            and self._silence_failover_clip_id != clip.id
        ):
            self._clear_silence_failover()

        if self._player.clip_id == clip.id:
            return
        if self._player.clip_id == ANTI_BIANCO_CLIP_ID:
            self._capture_anti_bianco_resume()
        try:
            if clip.is_live:
                self._player.play_live(
                    clip_id=clip.id,
                    device_path=clip.path or LIVE_PATH_DEFAULT,
                    peak_gain=clip.peak_gain,
                )
                self._playing_id = clip.id
                self._status = f"In onda LIVE: {clip.show_title}"
                return
            if clip.is_link:
                try:
                    self._player.play_stream(
                        clip.path,
                        clip_id=clip.id,
                        peak_gain=clip.peak_gain,
                    )
                except Exception as exc:  # noqa: BLE001
                    self._enter_link_failover(clip, str(exc))
                    return
                self._clear_link_failover()
                self._playing_id = clip.id
                self._status = f"In onda LINK: {clip.show_title}"
                return
            if clip.is_playlist:
                pl = Path(clip.path)
                if not pl.is_file():
                    self._status = f"Playlist mancante: {clip.path}"
                    return
                try:
                    tracks = parse_playlist(pl)
                    durs = track_durations_ms(tracks)
                except Exception as exc:  # noqa: BLE001
                    self._status = f"Playlist: {exc}"
                    return
                if not tracks:
                    self._status = f"Playlist vuota: {clip.path}"
                    return
                offset = max(0, int((now - clip.start_ts).total_seconds() * 1000))
                self._player.play_playlist(
                    [str(t) for t in tracks],
                    durs,
                    clip_id=clip.id,
                    peak_gain=clip.peak_gain,
                    start_offset_ms=offset,
                )
                self._playing_id = clip.id
                self._status = f"In onda PLAYLIST: {clip.show_title}"
                return
            path = Path(clip.path)
            if not path.is_file():
                self._status = f"File mancante: {clip.path}"
                return
            offset = int((now - clip.start_ts).total_seconds() * 1000)
            offset = max(0, min(offset, max(0, clip.duration_ms - 50)))
            self._player.play_file(
                str(path),
                clip_id=clip.id,
                peak_gain=clip.peak_gain,
                start_offset_ms=offset,
            )
            self._playing_id = clip.id
            self._status = f"In onda: {clip.show_title}"
        except Exception as exc:  # noqa: BLE001
            if clip.is_link:
                self._enter_link_failover(clip, str(exc))
            else:
                self._status = f"Errore avvio: {exc}"

    def _scheduler_anti_bianco(
        self, *, link_failover: bool = False, silence_failover: bool = False
    ) -> None:
        assert self._player is not None
        path_s = self._anti_bianco_path
        if not path_s:
            if self._player.clip_id is not None:
                self._player.stop()
                self._playing_id = None
            return
        pl = Path(path_s)
        if not pl.is_file():
            if self._player.clip_id is not None:
                self._player.stop()
                self._playing_id = None
            self._status = f"ANTI BIANCO: playlist mancante ({path_s})"
            return
        if self._player.clip_id == ANTI_BIANCO_CLIP_ID:
            return
        try:
            tracks = parse_playlist(pl)
            durs = track_durations_ms(tracks)
        except Exception as exc:  # noqa: BLE001
            self._status = f"ANTI BIANCO: {exc}"
            return
        if not tracks:
            self._status = f"ANTI BIANCO: playlist vuota ({pl.name})"
            return
        try:
            self._player.play_playlist(
                [str(t) for t in tracks],
                durs,
                clip_id=ANTI_BIANCO_CLIP_ID,
                peak_gain=1.0,
                start_offset_ms=max(0, int(self._anti_bianco_resume_ms)),
            )
            self._playing_id = None
            if silence_failover:
                self._status = f"ANTI BIANCO (silenzio): {pl.name}"
            elif link_failover:
                self._status = f"ANTI BIANCO (LINK offline): {pl.name}"
            else:
                self._status = f"ANTI BIANCO in onda: {pl.name}"
        except Exception as exc:  # noqa: BLE001
            self._status = f"ANTI BIANCO errore: {exc}"
