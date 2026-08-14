# -*- coding: utf-8 -*-
"""Stream MP3 dell'uscita audio locale (monitor Pulse) per ascolto da browser."""

from __future__ import annotations

import shutil
import subprocess
from typing import BinaryIO

from pulse_sources import default_output_monitor


def open_browser_listen_ffmpeg() -> subprocess.Popen[bytes]:
    """Avvia ffmpeg: Pulse monitor → MP3 su stdout (per HTTP /api/listen)."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg non trovato")
    device = default_output_monitor()
    if not device:
        raise RuntimeError("monitor uscita Pulse non trovato (pactl / PulseAudio?)")
    proc = subprocess.Popen(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-fflags",
            "+nobuffer",
            "-f",
            "pulse",
            "-i",
            device,
            "-ac",
            "2",
            "-ar",
            "44100",
            "-b:a",
            "128k",
            "-f",
            "mp3",
            "-",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    if proc.stdout is None:
        proc.kill()
        raise RuntimeError("ffmpeg: stdout non disponibile")
    return proc


def stop_listen_proc(proc: subprocess.Popen[bytes] | None) -> None:
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    for pipe in (proc.stdout, proc.stderr):
        if pipe is not None:
            try:
                pipe.close()
            except Exception:
                pass


def pipe_mp3_to(wfile: BinaryIO, proc: subprocess.Popen[bytes], *, chunk: int = 4096) -> None:
    """Copia stdout ffmpeg su wfile finché il client resta connesso."""
    assert proc.stdout is not None
    try:
        while True:
            data = proc.stdout.read(chunk)
            if not data:
                break
            wfile.write(data)
            try:
                wfile.flush()
            except Exception:
                break
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    finally:
        stop_listen_proc(proc)
