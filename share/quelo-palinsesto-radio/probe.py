# -*- coding: utf-8 -*-
"""Durata, peak-normalize e tag metadati via FFmpeg/ffprobe."""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
from pathlib import Path


def which(cmd: str) -> str | None:
    return shutil.which(cmd)


def _clean_tag(value: object) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    # ffprobe a volte restituisce liste
    if text.startswith("[") and text.endswith("]"):
        text = text.strip("[]\"' ")
    return text.replace("\n", " ").strip()


def split_title_description(raw: str) -> tuple[str, str]:
    """Se il tag è «Nome - Descrizione», spezza; altrimenti solo titolo."""
    text = _clean_tag(raw)
    if not text:
        return "", ""
    for sep in (" - ", " – ", " — "):
        if sep in text:
            left, right = text.split(sep, 1)
            return left.strip(), right.strip()
    return text, ""


def fallback_title_from_filename(path: Path) -> str:
    stem = path.stem.replace("_", " ").replace(".", " ").strip()
    stem = re.sub(r"\s+", " ", stem)
    return stem or path.name


def probe_tags(path: Path) -> tuple[str, str]:
    """Ritorna (title, description) dai tag del file."""
    ffprobe = which("ffprobe")
    if not ffprobe:
        return fallback_title_from_filename(path), ""
    try:
        raw = subprocess.check_output(
            [
                ffprobe,
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(path),
            ],
            text=True,
            timeout=60,
        )
        data = json.loads(raw)
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return fallback_title_from_filename(path), ""

    tags: dict[str, object] = {}
    fmt = data.get("format") or {}
    if isinstance(fmt.get("tags"), dict):
        tags.update({k.lower(): v for k, v in fmt["tags"].items()})
    for stream in data.get("streams") or []:
        if isinstance(stream, dict) and isinstance(stream.get("tags"), dict):
            for k, v in stream["tags"].items():
                tags.setdefault(k.lower(), v)

    title_raw = _clean_tag(
        tags.get("title")
        or tags.get("titolo")
        or tags.get("name")
        or tags.get("track")
    )
    desc_raw = _clean_tag(
        tags.get("description")
        or tags.get("comment")
        or tags.get("comments")
        or tags.get("synopsis")
        or tags.get("subtitle")
    )

    title, from_split = split_title_description(title_raw)
    description = desc_raw or from_split
    if not title:
        title = fallback_title_from_filename(path)
    return title, description


def probe_duration_ms(path: Path) -> int:
    ffprobe = which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe non trovato")
    raw = subprocess.check_output(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        text=True,
        timeout=120,
    ).strip()
    if not raw or raw == "N/A":
        raise RuntimeError("durata non disponibile")
    seconds = float(raw)
    if seconds <= 0:
        raise RuntimeError("durata non valida")
    return max(1, int(round(seconds * 1000)))


def probe_peak_gain(path: Path) -> float:
    """Gain lineare per portare il picco a 0 dBFS. Silenzio/errore → 1.0."""
    ffmpeg = which("ffmpeg")
    if not ffmpeg:
        return 1.0
    try:
        proc = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-i",
                str(path),
                "-af",
                "volumedetect",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        text = (proc.stderr or "") + (proc.stdout or "")
    except (subprocess.SubprocessError, OSError):
        return 1.0

    m = re.search(r"max_volume:\s*([+-]?\d+(?:\.\d+)?)\s*dB", text)
    if not m:
        return 1.0
    peak_db = float(m.group(1))
    if peak_db >= -0.05:
        return 1.0
    if peak_db <= -60.0:
        return 1.0
    gain = 10.0 ** (-peak_db / 20.0)
    return max(1.0, min(gain, 32.0))


def probe_audio(path: Path) -> tuple[int, float, str, str]:
    """Return (duration_ms, peak_gain, title, description)."""
    duration_ms = probe_duration_ms(path)
    peak_gain = probe_peak_gain(path)
    if not math.isfinite(peak_gain) or peak_gain <= 0:
        peak_gain = 1.0
    title, description = probe_tags(path)
    return duration_ms, peak_gain, title, description
