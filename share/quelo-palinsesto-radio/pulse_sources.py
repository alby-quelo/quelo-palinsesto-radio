# -*- coding: utf-8 -*-
"""Ingressi PulseAudio: elenco, porte, volume, default — Quelo-palinsesto-radio."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass


@dataclass
class PulseSource:
    name: str  # source oppure source@port
    description: str
    is_monitor: bool
    volume_pct: int  # 0..150 tipico
    muted: bool
    port: str | None = None
    port_active: bool = True
    source_name: str = ""

    def __post_init__(self) -> None:
        if not self.source_name:
            base, _ = split_device(self.name)
            self.source_name = base

    @property
    def label(self) -> str:
        kind = "Monitor uscita" if self.is_monitor else "Ingresso"
        desc = (self.description or "").strip() or self.name
        mute = " [MUTATO]" if self.muted else ""
        inactive = ""
        if self.port and not self.port_active and not self.is_monitor:
            inactive = " [non attiva]"
        return f"{kind}: {desc}{mute}{inactive}"


def _pactl(*args: str) -> str:
    """Sempre locale C: con LANG=it pactl stampa Sorgente/Nome e il parser fallisce."""
    pactl = shutil.which("pactl")
    if not pactl:
        return ""
    env = {**os.environ, "LC_ALL": "C", "LANG": "C"}
    try:
        return subprocess.check_output(
            [pactl, *args],
            text=True,
            timeout=15,
            stderr=subprocess.DEVNULL,
            env=env,
        )
    except (subprocess.SubprocessError, OSError):
        return ""


def _parse_volume_pct(volume_line: str) -> int:
    # es. Volume: front-left: 65536 / 100% / 0.00 dB, ...
    m = re.search(r"(\d+)%", volume_line)
    if not m:
        return 100
    return max(0, min(150, int(m.group(1))))


def _clean_port_label(raw: str) -> str:
    """Toglie coda ' (type: …)' dalla descrizione porta Pulse."""
    text = (raw or "").strip()
    if " (type:" in text:
        text = text.split(" (type:", 1)[0].strip()
    return text or raw.strip()


def split_device(device: str | None) -> tuple[str, str | None]:
    raw = (device or "default").strip()
    if raw.startswith("pulse:"):
        raw = raw[6:]
    raw = raw.strip() or "default"
    if raw != "default" and "@" in raw:
        src, port = raw.split("@", 1)
        return (src.strip() or "default", port.strip() or None)
    return raw, None


def set_source_port(source: str, port: str) -> None:
    src, _ = split_device(source)
    if not src or src == "default" or not port:
        return
    _pactl("set-source-port", src, port)


def list_pulse_sources(*, include_monitors: bool = True) -> list[PulseSource]:
    """Ingressi Pulse; se ci sono porte → una voce per porta (name=source@port)."""
    raw = _pactl("list", "sources")
    if not raw:
        return []

    sources: list[PulseSource] = []
    name = ""
    description = ""
    volume_pct = 100
    muted = False
    ports: list[tuple[str, str]] = []  # (port_id, label)
    active_port = ""
    in_ports = False

    def flush() -> None:
        nonlocal name, description, volume_pct, muted, ports, active_port, in_ports
        if not name:
            return
        is_mon = ".monitor" in name or "monitor" in description.lower()
        if is_mon or not ports:
            sources.append(
                PulseSource(
                    name=name,
                    description=description,
                    is_monitor=is_mon,
                    volume_pct=volume_pct,
                    muted=muted,
                    port=None,
                    port_active=True,
                    source_name=name,
                )
            )
        else:
            for port_id, port_label in ports:
                sources.append(
                    PulseSource(
                        name=f"{name}@{port_id}",
                        description=port_label or port_id,
                        is_monitor=False,
                        volume_pct=volume_pct,
                        muted=muted,
                        port=port_id,
                        port_active=(port_id == active_port),
                        source_name=name,
                    )
                )
        name = ""
        description = ""
        volume_pct = 100
        muted = False
        ports = []
        active_port = ""
        in_ports = False

    for line in raw.splitlines():
        if line.startswith("Source #"):
            flush()
            continue
        # Fine sezione Ports: riga non indentata (o Active Port)
        if in_ports and line and not line[0].isspace() and not line.startswith("\t"):
            in_ports = False

        stripped = line.strip()
        if stripped.startswith("Name:"):
            name = stripped.split(":", 1)[1].strip()
            in_ports = False
        elif stripped.startswith("Description:"):
            description = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("Mute:"):
            muted = stripped.split(":", 1)[1].strip().lower() in ("yes", "true", "1")
        elif stripped.startswith("Volume:"):
            volume_pct = _parse_volume_pct(stripped)
        elif stripped.startswith("Ports:"):
            in_ports = True
            ports = []
        elif stripped.startswith("Active Port:"):
            in_ports = False
            active_port = stripped.split(":", 1)[1].strip()
        elif in_ports and ":" in stripped:
            # analog-input-mic: Microphone (type: Mic, …)
            port_id, rest = stripped.split(":", 1)
            port_id = port_id.strip()
            if port_id:
                ports.append((port_id, _clean_port_label(rest)))

    flush()

    hw = [s for s in sources if not s.is_monitor]
    mon = [s for s in sources if s.is_monitor]
    if include_monitors:
        return hw + mon
    return hw


def get_default_source() -> str:
    name = _pactl("get-default-source").strip()
    return name or "default"


def get_default_sink() -> str:
    name = _pactl("get-default-sink").strip()
    return name


def default_output_monitor() -> str:
    """Source Pulse che cattura ciò che esce dagli altoparlanti (sink.monitor)."""
    sink = get_default_sink()
    if sink:
        return f"{sink}.monitor"
    # Fallback: primo monitor elencato
    for item in list_pulse_sources(include_monitors=True):
        if item.is_monitor:
            return item.source_name or item.name
    return ""


def set_default_source(name: str) -> bool:
    src, port = split_device(name)
    if not src or src == "default":
        return False
    if port:
        set_source_port(src, port)
    _pactl("set-default-source", src)
    return get_default_source() == src


def set_source_mute(name: str, muted: bool) -> None:
    src, _ = split_device(name)
    if not src or src == "default":
        src = get_default_source()
    _pactl("set-source-mute", src, "1" if muted else "0")


def set_source_volume_pct(name: str, pct: int) -> None:
    src, _ = split_device(name)
    if not src or src == "default":
        src = get_default_source()
    pct = max(0, min(150, int(pct)))
    _pactl("set-source-volume", src, f"{pct}%")


def get_source_volume_pct(name: str) -> int:
    src, _ = split_device(name)
    if not src or src == "default":
        src = get_default_source()
    for item in list_pulse_sources(include_monitors=True):
        if item.source_name == src or item.name == src:
            return item.volume_pct
    return 100


def ensure_source_ready(name: str) -> str:
    """Attiva porta se presente, smuta, ritorna SOLO source_name (per pulsesrc)."""
    src, port = split_device(name)
    if not src or src == "default":
        src = get_default_source() or "default"
    if port and src and src != "default":
        set_source_port(src, port)
    if src and src != "default":
        set_source_mute(src, False)
    return src


def normalize_device(device: str | None) -> str:
    """Mantiene eventuale @port (encoding Quelo)."""
    raw = (device or "default").strip()
    if raw.startswith("pulse:"):
        raw = raw[6:]
    return raw or "default"


def to_pulse_path(device: str | None) -> str:
    return f"pulse:{normalize_device(device)}"
