# -*- coding: utf-8 -*-
"""Player GStreamer: file + ingresso live (pulsesrc) per Quelo-palinsesto-radio."""

from __future__ import annotations

import math
from typing import Callable

from pulse_sources import ensure_source_ready

try:
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import GLib, Gst  # type: ignore
except (ImportError, ValueError) as exc:  # pragma: no cover
    Gst = None  # type: ignore
    GLib = None  # type: ignore
    _GI_ERROR = exc
else:
    _GI_ERROR = None


def gstreamer_available() -> bool:
    return Gst is not None


def pump_glib_once() -> None:
    if GLib is None:
        return
    ctx = GLib.MainContext.default()
    while ctx.pending():
        ctx.iteration(False)


def _pulse_device_from_path(path: str) -> str | None:
    raw = (path or "").strip()
    if raw.startswith("pulse:"):
        raw = raw[6:]
    if not raw or raw == "default":
        return None
    return raw


def extract_peak_db(struct) -> float | None:
    """Picco dBFS dal messaggio GStreamer level."""
    try:
        ok, peak = struct.get_list("peak")
        if ok and peak is not None:
            values = [float(peak.get_nth(i)) for i in range(peak.n_values)]
            if values:
                return max(values)
    except (TypeError, ValueError, AttributeError):
        pass
    try:
        peak_val = struct.get_value("peak")
        if peak_val:
            return max(float(x) for x in peak_val)
    except (TypeError, ValueError):
        pass
    try:
        ok, rms = struct.get_list("rms")
        if ok and rms is not None:
            values = [float(rms.get_nth(i)) for i in range(rms.n_values)]
            if values:
                return max(values)
    except (TypeError, ValueError, AttributeError):
        pass
    return None


class AudioPlayer:
    """Riproduzione file o loopback ingresso → uscita, con VU (dBFS) e volume.

    on_level riceve dBFS (es. -12.0); silenzio/stop → -120.0
    """

    def __init__(
        self,
        on_level: Callable[[float], None] | None = None,
        on_eos: Callable[[], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        if Gst is None:
            raise RuntimeError(
                f"GStreamer/PyGObject non disponibile: {_GI_ERROR}"
            )
        Gst.init(None)
        self._on_level = on_level
        self._on_eos = on_eos
        self._on_error = on_error
        self._pipeline = None
        self._volume_el = None
        self._agc_el = None
        self._peak_gain = 1.0
        self._master = 1.0
        self._agc_gain = 1.0
        self._clip_id: int | None = None
        self._mode: str | None = None
        self._bus_handler = None
        self._playlist_tracks: list[str] = []
        self._playlist_durs: list[int] = []
        self._playlist_idx: int = 0
        self._keep_playlist: bool = False

    @property
    def clip_id(self) -> int | None:
        return self._clip_id

    @property
    def mode(self) -> str | None:
        return self._mode

    @property
    def is_playing(self) -> bool:
        if self._pipeline is None:
            return False
        _, state, _ = self._pipeline.get_state(0)
        return state == Gst.State.PLAYING

    def playlist_position_ms(self) -> int:
        """Offset ms lungo la playlist concatenata (0 se non in mode playlist)."""
        if self._mode != "playlist" or not self._playlist_tracks:
            return 0
        durs = self._playlist_durs
        idx = max(0, min(self._playlist_idx, len(self._playlist_tracks) - 1))
        base = 0
        if durs:
            base = int(sum(durs[:idx]))
        pos = 0
        if self._pipeline is not None and Gst is not None:
            try:
                ok, ns = self._pipeline.query_position(Gst.Format.TIME)
                if ok and ns is not None and int(ns) >= 0:
                    pos = int(int(ns) / 1_000_000)
            except Exception:
                pos = 0
        return max(0, base + pos)

    def set_master_volume(self, master: float) -> None:
        self._master = max(0.0, min(1.0, float(master)))
        self._apply_volume()

    def _apply_volume(self) -> None:
        if self._mode == "live":
            if self._agc_el is not None:
                self._agc_el.set_property("volume", self._agc_gain)
            if self._volume_el is not None:
                self._volume_el.set_property("volume", self._master)
            return
        if self._volume_el is None:
            return
        # file + stream + playlist: peak_gain * master
        self._volume_el.set_property("volume", self._peak_gain * self._master)

    def stop(self) -> None:
        self._playlist_tracks = []
        self._playlist_durs = []
        self._keep_playlist = False
        self._teardown()
        self._clip_id = None
        self._mode = None
        if self._on_level:
            self._on_level(-120.0)

    def _teardown(self) -> None:
        if self._pipeline is not None:
            bus = self._pipeline.get_bus()
            if self._bus_handler is not None:
                try:
                    bus.disconnect(self._bus_handler)
                except Exception:
                    pass
                self._bus_handler = None
            try:
                bus.remove_signal_watch()
            except Exception:
                pass
            self._pipeline.set_state(Gst.State.NULL)
            self._pipeline = None
        self._volume_el = None
        self._agc_el = None
        if not self._keep_playlist:
            self._playlist_tracks = []
            self._playlist_durs = []
            self._playlist_idx = 0


    def _attach_bus(self, pipeline) -> None:
        bus = pipeline.get_bus()
        bus.add_signal_watch()
        self._bus_handler = bus.connect("message", self._on_bus_message)

    def _make_audio_filter(self):
        filt = Gst.Bin.new("quelo-af")
        vol = Gst.ElementFactory.make("volume", "vol")
        lvl = Gst.ElementFactory.make("level", "lvl")
        if vol is None or lvl is None:
            raise RuntimeError("elementi GStreamer volume/level mancanti")
        lvl.set_property("post-messages", True)
        lvl.set_property("interval", 50_000_000)  # 50 ms
        filt.add(vol)
        filt.add(lvl)
        if not vol.link(lvl):
            raise RuntimeError("link volume→level fallito")
        filt.add_pad(Gst.GhostPad.new("sink", vol.get_static_pad("sink")))
        filt.add_pad(Gst.GhostPad.new("src", lvl.get_static_pad("src")))
        self._volume_el = vol
        self._agc_el = None
        return filt

    def _configure_playbin(self, playbin) -> None:
        """Uscita Pulse esplicita + VU (audio-filter) + niente video."""
        playbin.set_property("audio-filter", self._make_audio_filter())
        fake = Gst.ElementFactory.make("fakesink", "quelo-vsink")
        if fake is not None:
            fake.set_property("sync", True)
            playbin.set_property("video-sink", fake)
        sink = Gst.ElementFactory.make("pulsesink", "quelo-asink")
        if sink is not None:
            # sync=true: A/V sync non serve; false riduce underrun su live-ish
            try:
                sink.set_property("sync", True)
            except Exception:
                pass
            playbin.set_property("audio-sink", sink)
        self._apply_volume()

    def play_file(
        self,
        path: str,
        *,
        clip_id: int,
        peak_gain: float = 1.0,
        start_offset_ms: int = 0,
    ) -> None:
        self._teardown()
        self._clip_id = clip_id
        self._mode = "file"
        self._peak_gain = max(0.0, float(peak_gain))
        self._agc_gain = 1.0

        playbin = Gst.ElementFactory.make("playbin", "playbin")
        if playbin is None:
            raise RuntimeError("playbin non disponibile")
        playbin.set_property("uri", Gst.filename_to_uri(path))
        self._configure_playbin(playbin)
        self._pipeline = playbin
        self._attach_bus(playbin)

        playbin.set_state(Gst.State.PAUSED)
        playbin.get_state(5 * Gst.SECOND)
        if start_offset_ms > 0:
            ns = int(start_offset_ms) * 1_000_000
            playbin.seek_simple(
                Gst.Format.TIME,
                Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                ns,
            )
        playbin.set_state(Gst.State.PLAYING)

    def play_live(
        self,
        *,
        clip_id: int,
        device_path: str = "pulse:default",
        peak_gain: float = 1.0,
    ) -> None:
        """Ingresso Pulse → AGC → master → uscita; VU da dinamica pre-AGC."""
        del peak_gain  # LIVE: AGC fisso verso 0 dB
        self._teardown()
        self._clip_id = clip_id
        self._mode = "live"
        self._peak_gain = 1.0
        self._agc_gain = 4.0  # partenza ragionevole; AGC raffina

        device = _pulse_device_from_path(device_path)
        if device is None:
            device = ensure_source_ready("default")
            if device == "default":
                device = None
        else:
            device = ensure_source_ready(device)

        # lvl_in → VU UI; agc → normalize; lvl_agc → feedback AGC; vol → master
        pipeline = Gst.parse_launch(
            "pulsesrc name=src "
            "! audioconvert ! audioresample "
            "! level name=lvl_in interval=50000000 post-messages=true "
            "! volume name=agc "
            "! level name=lvl_agc interval=50000000 post-messages=true "
            "! volume name=vol "
            "! pulsesink name=sink sync=false"
        )
        src = pipeline.get_by_name("src")
        if device is not None:
            src.set_property("device", device)
        self._agc_el = pipeline.get_by_name("agc")
        self._volume_el = pipeline.get_by_name("vol")
        self._apply_volume()
        self._pipeline = pipeline
        self._attach_bus(pipeline)
        pipeline.set_state(Gst.State.PLAYING)

    def set_live_sink_muted(self, muted: bool) -> None:
        """LIVE: azzera solo l'uscita pulsesink, lasciando attivi level/AGC (monitor)."""
        if self._mode != "live" or self._pipeline is None:
            return
        sink = self._pipeline.get_by_name("sink")
        if sink is None:
            return
        try:
            sink.set_property("volume", 0.0 if muted else 1.0)
        except Exception:
            pass

    def play_stream(
        self,
        url: str,
        *,
        clip_id: int,
        peak_gain: float = 1.0,
    ) -> None:
        """Stream HTTP/HTTPS via curlhttpsrc (ssl-strict=False); fallback playbin."""
        raw = (url or "").strip()
        low = raw.lower()
        if not (low.startswith("http://") or low.startswith("https://")):
            raise ValueError("URL stream non valido (serve http:// o https://)")
        self._keep_playlist = False
        self._teardown()
        self._clip_id = clip_id
        self._mode = "stream"
        self._peak_gain = max(0.0, float(peak_gain))
        self._agc_gain = 1.0

        curl = Gst.ElementFactory.make("curlhttpsrc", "src")
        if curl is None:
            # Fallback: playbin (HTTPS può fallire senza GIO TLS)
            playbin = Gst.ElementFactory.make("playbin", "playbin")
            if playbin is None:
                raise RuntimeError("né curlhttpsrc né playbin disponibili")
            playbin.set_property("uri", raw)
            self._configure_playbin(playbin)
            self._pipeline = playbin
            self._attach_bus(playbin)
            playbin.set_state(Gst.State.PLAYING)
            return

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
        sink = Gst.ElementFactory.make("pulsesink", "sink")
        if not all((decode, conv, ar, vol, lvl, sink)):
            raise RuntimeError("elementi GStreamer stream mancanti")
        lvl.set_property("post-messages", True)
        lvl.set_property("interval", 50_000_000)
        sink.set_property("sync", False)

        pipeline = Gst.Pipeline.new("quelo-stream")
        for el in (curl, decode, conv, ar, vol, lvl, sink):
            pipeline.add(el)
        if not curl.link(decode):
            raise RuntimeError("link curl→decode fallito")

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
            sp = conv.get_static_pad("sink")
            if sp is None or sp.is_linked():
                return
            pad.link(sp)

        decode.connect("pad-added", on_pad)
        if not conv.link(ar) or not ar.link(vol) or not vol.link(lvl) or not lvl.link(sink):
            raise RuntimeError("link audio stream fallito")
        self._volume_el = vol
        self._agc_el = None
        self._apply_volume()
        self._pipeline = pipeline
        self._attach_bus(pipeline)
        pipeline.set_state(Gst.State.PLAYING)

    def play_playlist(
        self,
        tracks: list[str],
        durations_ms: list[int] | None = None,
        *,
        clip_id: int,
        peak_gain: float = 1.0,
        start_offset_ms: int = 0,
    ) -> None:
        """Sequenza file locali con loop fino a stop schedulato."""
        if not tracks:
            raise ValueError("playlist vuota")
        durs = list(durations_ms or [])
        while len(durs) < len(tracks):
            durs.append(180_000)
        durs = durs[: len(tracks)]

        rem = max(0, int(start_offset_ms))
        idx = 0
        # offset lungo la timeline concatenata (modulo durata totale)
        total = sum(durs) or 1
        rem = rem % total
        while idx < len(durs) and rem >= durs[idx]:
            rem -= durs[idx]
            idx += 1
        if idx >= len(tracks):
            idx = 0
            rem = 0

        self._playlist_tracks = list(tracks)
        self._playlist_durs = durs
        self._playlist_idx = idx
        self._clip_id = clip_id
        self._mode = "playlist"
        self._peak_gain = max(0.0, float(peak_gain))
        self._agc_gain = 1.0
        self._start_playlist_track(idx, rem)

    def _start_playlist_track(self, idx: int, offset_ms: int = 0) -> None:
        path = self._playlist_tracks[idx]
        self._playlist_idx = idx
        self._keep_playlist = True
        self._teardown()
        self._keep_playlist = False
        self._mode = "playlist"

        playbin = Gst.ElementFactory.make("playbin", "playbin")
        if playbin is None:
            raise RuntimeError("playbin non disponibile")
        playbin.set_property("uri", Gst.filename_to_uri(path))
        self._configure_playbin(playbin)
        self._pipeline = playbin
        self._attach_bus(playbin)
        playbin.set_state(Gst.State.PAUSED)
        playbin.get_state(5 * Gst.SECOND)
        if offset_ms > 0:
            ns = int(offset_ms) * 1_000_000
            playbin.seek_simple(
                Gst.Format.TIME,
                Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                ns,
            )
        playbin.set_state(Gst.State.PLAYING)

    def _playlist_advance(self) -> None:
        if not self._playlist_tracks:
            return
        nxt = (self._playlist_idx + 1) % len(self._playlist_tracks)
        try:
            self._start_playlist_track(nxt, 0)
        except Exception as exc:  # noqa: BLE001
            if self._on_error:
                self._on_error(str(exc))

    def _update_live_agc(self, peak_db: float) -> None:
        """Porta il picco del programma verso 0 dBFS (dopo AGC, prima del master)."""
        if peak_db <= -90.0:
            return
        err_db = max(-6.0, min(6.0, 0.0 - peak_db))
        factor = 10.0 ** (err_db / 20.0)
        self._agc_gain *= 0.90 + 0.10 * factor
        self._agc_gain = max(0.5, min(32.0, self._agc_gain))
        self._apply_volume()

    def _on_bus_message(self, _bus, message) -> None:
        t = message.type
        if t == Gst.MessageType.EOS:
            if self._mode == "playlist" and self._playlist_tracks:
                self._playlist_advance()
                return
            self._teardown()
            self._clip_id = None
            self._mode = None
            if self._on_level:
                self._on_level(-120.0)
            if self._on_eos:
                self._on_eos()
        elif t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            msg = err.message if err else "errore GStreamer"
            if debug:
                msg = f"{msg} ({debug})"
            self._teardown()
            self._clip_id = None
            self._mode = None
            if self._on_level:
                self._on_level(-120.0)
            if self._on_error:
                self._on_error(msg)
        elif t == Gst.MessageType.ELEMENT:
            struct = message.get_structure()
            if struct is None or not struct.has_name("level"):
                return
            db = extract_peak_db(struct)
            if db is None:
                return
            src_name = ""
            try:
                if message.src is not None:
                    src_name = message.src.get_name() or ""
            except Exception:
                src_name = ""

            if self._mode == "live":
                if src_name == "lvl_agc":
                    self._update_live_agc(float(db))
                    return
                if src_name == "lvl_in":
                    if self._master <= 0.0:
                        ui_db = -120.0
                    else:
                        ui_db = float(db) + 20.0 * math.log10(self._master)
                    if self._on_level:
                        self._on_level(ui_db)
                    return
                return

            if self._on_level:
                self._on_level(float(db))
