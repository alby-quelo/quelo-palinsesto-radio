# -*- coding: utf-8 -*-
"""Entry point Quelo-palinsesto-radio (desktop Qt oppure --web-only)."""

from __future__ import annotations

import argparse
import sys
import time


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="quelo-palinsesto-radio",
        description=(
            "Scheduler/player palinsesto radio settimanale. "
            "Desktop PyQt oppure controllo remoto HTTP (--web-only)."
        ),
    )
    parser.add_argument(
        "--port",
        "-p",
        type=int,
        default=None,
        help="Porta web (default 8890)",
    )
    parser.add_argument(
        "--bind",
        default=None,
        help="Bind HTTP (default 0.0.0.0 = LAN)",
    )
    parser.add_argument(
        "--web-only",
        action="store_true",
        help="Solo interfaccia web (senza desktop Qt)",
    )
    parser.add_argument(
        "--version",
        action="version",
        version="quelo-palinsesto-radio 1.1",
    )
    args = parser.parse_args(argv)

    if args.web_only:
        from engine import PalinsestoEngine
        from player import gstreamer_available
        from web import DEFAULT_WEB_PORT, start_web_server, stop_web_server, web_urls

        if not gstreamer_available():
            print("Errore: GStreamer/PyGObject non disponibile", file=sys.stderr)
            return 1

        engine = PalinsestoEngine()
        port = int(args.port) if args.port is not None else DEFAULT_WEB_PORT
        bind = (args.bind if args.bind is not None else "0.0.0.0").strip() or "0.0.0.0"
        holder: dict = {}
        try:
            server = start_web_server(
                engine, host=bind, port=port, attach=True, holder=holder
            )
        except OSError as exc:
            print(f"Errore avvio web: {exc}", file=sys.stderr)
            engine.shutdown()
            return 1

        try:
            st = engine.start()
            print(f"Palinsesto in onda (autostart interno): {st.get('status', '')}")
        except Exception as exc:  # noqa: BLE001
            print(f"Avviso: avvio palinsesto fallito: {exc}", file=sys.stderr)

        print(f"Web UI attiva su {bind}:{port}")
        for url in web_urls(bind, port):
            print(f"  {url}")
        print("Modalità web-only — Ctrl+C per uscire")
        try:
            while True:
                engine.poll()
                time.sleep(0.05)
        except KeyboardInterrupt:
            print()
        finally:
            stop_web_server(holder.get("server") or server)
            engine.shutdown()
        return 0

    # Desktop Qt (comportamento storico)
    from ui import run_app

    return run_app()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
