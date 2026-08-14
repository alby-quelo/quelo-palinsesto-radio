# -*- coding: utf-8 -*-
"""Parse M3U/PLS e probe durata playlist — Quelo-palinsesto-radio."""

from __future__ import annotations

from pathlib import Path

from probe import probe_audio, probe_duration_ms

AUDIO_SUFFIXES = {
    ".wav",
    ".mp3",
    ".flac",
    ".ogg",
    ".opus",
    ".m4a",
    ".aac",
    ".wma",
    ".aiff",
    ".aif",
}


def _is_http(line: str) -> bool:
    low = line.lower()
    return low.startswith("http://") or low.startswith("https://")


def parse_playlist(path: Path) -> list[Path]:
    """Elenco file audio locali dalla playlist; URL http/https saltati."""
    path = path.resolve()
    base = path.parent
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = [ln.strip() for ln in text.splitlines()]
    suffix = path.suffix.lower()
    out: list[Path] = []

    if suffix == ".pls":
        # File1=..., Title1=...
        entries: dict[int, str] = {}
        for ln in lines:
            if "=" not in ln:
                continue
            key, val = ln.split("=", 1)
            key = key.strip().lower()
            val = val.strip()
            if key.startswith("file") and key[4:].isdigit():
                entries[int(key[4:])] = val
        for i in sorted(entries):
            raw = entries[i]
            if not raw or _is_http(raw):
                continue
            cand = Path(raw)
            if not cand.is_absolute():
                cand = (base / raw).resolve()
            if cand.is_file() and cand.suffix.lower() in AUDIO_SUFFIXES:
                out.append(cand)
        return out

    # M3U / M3U8
    for ln in lines:
        if not ln or ln.startswith("#"):
            continue
        if _is_http(ln):
            continue
        cand = Path(ln)
        if not cand.is_absolute():
            cand = (base / ln).resolve()
        if cand.is_file() and cand.suffix.lower() in AUDIO_SUFFIXES:
            out.append(cand)
    return out


def track_durations_ms(tracks: list[Path]) -> list[int]:
    durs: list[int] = []
    for t in tracks:
        try:
            durs.append(probe_duration_ms(t))
        except Exception:
            durs.append(180_000)  # 3 min fallback
    return durs


def probe_playlist(path: Path) -> tuple[int, float, str, str, list[Path], list[int]]:
    """
    Ritorna:
      total_ms, peak_gain, title, description, tracks, durations_ms
    Se probe fallisce sulla durata totale → 1h.
    """
    path = Path(path)
    tracks = parse_playlist(path)
    if not tracks:
        raise ValueError(f"Playlist vuota o senza file audio locali: {path}")

    durs = track_durations_ms(tracks)
    total = sum(durs)
    if total < 1000:
        total = 3_600_000

    # peak_gain: minimo tra i file (più conservativo) oppure 1.0
    peak = 1.0
    try:
        gains = []
        for t in tracks[:12]:  # limiti tempo probe
            _d, g, _ti, _de = probe_audio(t)
            gains.append(g)
        if gains:
            peak = min(gains)
    except Exception:
        peak = 1.0

    title = path.stem.replace("_", " ").strip() or path.name
    description = f"{len(tracks)} brani"
    return total, peak, title, description, tracks, durs
