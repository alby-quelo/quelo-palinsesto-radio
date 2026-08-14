# -*- coding: utf-8 -*-
"""Monitor livello (VU) per ingressi Pulse, stream URL e file — Quelo-palinsesto-radio."""

from __future__ import annotations

from typing import Callable

try:
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst  # type: ignore
except (ImportError, ValueError) as exc:  # pragma: no cover
    Gst = None  # type: ignore
    _GI_ERROR = exc
else:
    _GI_ERROR = None

from player import extract_peak_db, gstreamer_available


def _link_decodebin_to_audio(decode, next_el) -> None:
    def on_pad(_dec, pad) -> None:
        caps = pad.get_current_caps()
        if caps is None or caps.is_empty():
            caps = pad.query_caps(None)
        name = ""
        try:
            if caps and not caps.is_empty():
                name = caps.get_structure(0).get_name() or ""
        except Exception:
            name = ""
        if name and not name.startswith("audio/"):
            return
        sink = next_el.get_static_pad("sink")
        if sink is None or sink.is_linked():
            return
        pad.link(sink)

    decode.connect("pad-added", on_pad)


class _TapBusMixin:
    """Poll bus GStreamer (affidabile anche se signal_watch non consegna)."""

    _pipeline = None
    _on_db = None
    _on_error = None
    _on_eos = None
    _last_db: float = -120.0

    def poll(self) -> float | None:
        """Drenare messaggi bus; ritorna ultimo dBFS se c'è un level nuovo."""
        if self._pipeline is None or Gst is None:
            return None
        bus = self._pipeline.get_bus()
        got = False
        while True:
            msg = bus.pop()
            if msg is None:
                break
            t = msg.type
            if t == Gst.MessageType.ELEMENT:
                struct = msg.get_structure()
                if struct is not None and struct.has_name("level"):
                    db = extract_peak_db(struct)
                    if db is not None:
                        self._last_db = float(db)
                        got = True
                        if self._on_db:
                            self._on_db(self._last_db)
            elif t == Gst.MessageType.ERROR:
                err, debug = msg.parse_error()
                text = err.message if err else "errore monitor"
                if debug:
                    text = f"{text} ({debug})"
                self._last_db = -120.0
                got = True
                if self._on_error:
                    self._on_error(text)
                if self._on_db:
                    self._on_db(-120.0)
            elif t == Gst.MessageType.EOS:
                self._last_db = -120.0
                got = True
                if self._on_eos:
                    self._on_eos()
                elif self._on_db:
                    self._on_db(-120.0)
        return self._last_db if got else None

    def _teardown_bus(self) -> None:
        if self._pipeline is None:
            return
        bus = self._pipeline.get_bus()
        handler = getattr(self, "_bus_handler", None)
        if handler is not None:
            try:
                bus.disconnect(handler)
            except Exception:
                pass
            self._bus_handler = None
        try:
            bus.remove_signal_watch()
        except Exception:
            pass
        self._pipeline.set_state(Gst.State.NULL)
        self._pipeline = None


class SourceLevelTap(_TapBusMixin):
    """pulsesrc → level → fakesink: misura dBFS senza riprodurre in uscita."""

    def __init__(
        self,
        device: str,
        on_db: Callable[[float], None],
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        if Gst is None or not gstreamer_available():
            raise RuntimeError(f"GStreamer non disponibile: {_GI_ERROR}")
        Gst.init(None)
        self._on_db = on_db
        self._on_error = on_error
        self._on_eos = None
        self._device = device
        self._bus_handler = None
        self._pipeline = None
        self._last_db = -120.0
        pipeline = Gst.parse_launch(
            "pulsesrc name=src "
            "! audioconvert ! audioresample "
            "! level name=lvl interval=50000000 post-messages=true "
            "! fakesink sync=false"
        )
        src = pipeline.get_by_name("src")
        if device:
            src.set_property("device", device)
        bus = pipeline.get_bus()
        bus.add_signal_watch()
        self._bus_handler = bus.connect("message", self._on_bus_message)
        self._pipeline = pipeline
        pipeline.set_state(Gst.State.PLAYING)

    def stop(self) -> None:
        cb = self._on_db
        self._on_db = None
        self._teardown_bus()
        if cb:
            cb(-120.0)

    def _on_bus_message(self, _bus, message) -> None:
        t = message.type
        if t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            msg = err.message if err else "errore monitor"
            if debug:
                msg = f"{msg} ({debug})"
            if self._on_error:
                self._on_error(msg)
            self._last_db = -120.0
            if self._on_db:
                self._on_db(-120.0)
        elif t == Gst.MessageType.ELEMENT:
            struct = message.get_structure()
            if struct is not None and struct.has_name("level"):
                db = extract_peak_db(struct)
                if db is not None:
                    self._last_db = float(db)
                    if self._on_db:
                        self._on_db(self._last_db)


class StreamLevelTap(_TapBusMixin):
    """curlhttpsrc → decode → level → fakesink (anteprima VU senza uscita)."""

    def __init__(
        self,
        url: str,
        on_db: Callable[[float], None],
        on_error: Callable[[str], None] | None = None,
        volume: float = 1.0,
    ) -> None:
        if Gst is None or not gstreamer_available():
            raise RuntimeError(f"GStreamer non disponibile: {_GI_ERROR}")
        Gst.init(None)
        raw = (url or "").strip()
        low = raw.lower()
        if not (low.startswith("http://") or low.startswith("https://")):
            raise ValueError("URL non valido")
        self._on_db = on_db
        self._on_error = on_error
        self._on_eos = None
        self._bus_handler = None
        self._pipeline = None
        self._volume_el = None
        self._last_db = -120.0

        curl = Gst.ElementFactory.make("curlhttpsrc", "src")
        if curl is None:
            raise RuntimeError("curlhttpsrc non disponibile (gstreamer1.0-plugins-bad)")
        curl.set_property("location", raw)
        try:
            curl.set_property("ssl-strict", False)
        except Exception:
            pass

        decode = Gst.ElementFactory.make("decodebin", "dec")
        conv = Gst.ElementFactory.make("audioconvert", "ac")
        ar = Gst.ElementFactory.make("audioresample", "ar")
        vol = Gst.ElementFactory.make("volume", "vol")
        lvl = Gst.ElementFactory.make("level", "lvl")
        sink = Gst.ElementFactory.make("fakesink", "sink")
        if not all((decode, conv, ar, vol, lvl, sink)):
            raise RuntimeError("elementi GStreamer mancanti per StreamLevelTap")
        lvl.set_property("post-messages", True)
        lvl.set_property("interval", 50_000_000)
        sink.set_property("sync", False)
        vol.set_property("volume", max(0.0, float(volume)))

        pipeline = Gst.Pipeline.new("stream-tap")
        for el in (curl, decode, conv, ar, vol, lvl, sink):
            pipeline.add(el)
        if not curl.link(decode):
            raise RuntimeError("link curl→decode fallito")
        _link_decodebin_to_audio(decode, conv)
        if not conv.link(ar) or not ar.link(vol) or not vol.link(lvl) or not lvl.link(sink):
            raise RuntimeError("link audio StreamLevelTap fallito")

        self._volume_el = vol
        bus = pipeline.get_bus()
        bus.add_signal_watch()
        self._bus_handler = bus.connect("message", self._on_bus_message)
        self._pipeline = pipeline
        pipeline.set_state(Gst.State.PLAYING)

    def set_volume(self, gain: float) -> None:
        if self._volume_el is not None:
            self._volume_el.set_property("volume", max(0.0, float(gain)))

    def stop(self) -> None:
        cb = self._on_db
        self._on_db = None
        self._teardown_bus()
        self._volume_el = None
        if cb:
            cb(-120.0)

    def _on_bus_message(self, _bus, message) -> None:
        t = message.type
        if t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            msg = err.message if err else "errore stream"
            if debug:
                msg = f"{msg} ({debug})"
            if self._on_error:
                self._on_error(msg)
            self._last_db = -120.0
            if self._on_db:
                self._on_db(-120.0)
        elif t == Gst.MessageType.ELEMENT:
            struct = message.get_structure()
            if struct is not None and struct.has_name("level"):
                db = extract_peak_db(struct)
                if db is not None:
                    self._last_db = float(db)
                    if self._on_db:
                        self._on_db(self._last_db)


class FileLevelTap(_TapBusMixin):
    """uridecodebin → level → fakesink sync (monitor file senza uscita audio)."""

    def __init__(
        self,
        path: str,
        on_db: Callable[[float], None],
        on_error: Callable[[str], None] | None = None,
        on_eos: Callable[[], None] | None = None,
        start_offset_ms: int = 0,
    ) -> None:
        if Gst is None or not gstreamer_available():
            raise RuntimeError(f"GStreamer non disponibile: {_GI_ERROR}")
        Gst.init(None)
        self._on_db = on_db
        self._on_error = on_error
        self._on_eos = on_eos
        self._bus_handler = None
        self._pipeline = None
        self._last_db = -120.0

        uri = Gst.filename_to_uri(path)
        decode = Gst.ElementFactory.make("uridecodebin", "dec")
        conv = Gst.ElementFactory.make("audioconvert", "ac")
        ar = Gst.ElementFactory.make("audioresample", "ar")
        lvl = Gst.ElementFactory.make("level", "lvl")
        sink = Gst.ElementFactory.make("fakesink", "sink")
        if not all((decode, conv, ar, lvl, sink)):
            raise RuntimeError("elementi GStreamer mancanti per FileLevelTap")
        decode.set_property("uri", uri)
        lvl.set_property("post-messages", True)
        lvl.set_property("interval", 50_000_000)
        sink.set_property("sync", True)

        pipeline = Gst.Pipeline.new("file-tap")
        for el in (decode, conv, ar, lvl, sink):
            pipeline.add(el)
        _link_decodebin_to_audio(decode, conv)
        if not conv.link(ar) or not ar.link(lvl) or not lvl.link(sink):
            raise RuntimeError("link audio FileLevelTap fallito")

        bus = pipeline.get_bus()
        bus.add_signal_watch()
        self._bus_handler = bus.connect("message", self._on_bus_message)
        self._pipeline = pipeline
        pipeline.set_state(Gst.State.PAUSED)
        pipeline.get_state(5 * Gst.SECOND)
        if start_offset_ms > 0:
            ns = int(start_offset_ms) * 1_000_000
            pipeline.seek_simple(
                Gst.Format.TIME,
                Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                ns,
            )
        pipeline.set_state(Gst.State.PLAYING)

    def stop(self) -> None:
        cb = self._on_db
        self._on_db = None
        self._on_eos = None
        self._teardown_bus()
        if cb:
            cb(-120.0)

    def _on_bus_message(self, _bus, message) -> None:
        t = message.type
        if t == Gst.MessageType.EOS:
            self._last_db = -120.0
            if self._on_eos:
                self._on_eos()
            elif self._on_db:
                self._on_db(-120.0)
        elif t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            msg = err.message if err else "errore file"
            if debug:
                msg = f"{msg} ({debug})"
            if self._on_error:
                self._on_error(msg)
            self._last_db = -120.0
            if self._on_db:
                self._on_db(-120.0)
        elif t == Gst.MessageType.ELEMENT:
            struct = message.get_structure()
            if struct is not None and struct.has_name("level"):
                db = extract_peak_db(struct)
                if db is not None:
                    self._last_db = float(db)
                    if self._on_db:
                        self._on_db(self._last_db)
