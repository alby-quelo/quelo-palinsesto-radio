# -*- coding: utf-8 -*-
"""GUI settimanale Quelo-palinsesto-radio."""

from __future__ import annotations

import math
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

from PyQt6.QtCore import QDate, QEvent, QPoint, QRect, QSize, Qt, QTime, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import (
    QApplication,
    QColorDialog,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from db import (
    DEFAULT_LINK_COLOR,
    DEFAULT_LIVE_COLOR,
    DEFAULT_PLAYLIST_COLOR,
    LIVE_PATH_DEFAULT,
    Clip,
    OverlapError,
    PalinsestoDB,
    link_display_name,
    normalize_color,
    normalize_stream_url,
)
from mixer import FileLevelTap, SourceLevelTap, StreamLevelTap
from player import AudioPlayer, gstreamer_available, pump_glib_once
from playlist import parse_playlist, probe_playlist, track_durations_ms
from probe import probe_audio
from pulse_sources import (
    PulseSource,
    ensure_source_ready,
    get_default_source,
    get_source_volume_pct,
    list_pulse_sources,
    normalize_device,
    set_default_source,
    set_source_mute,
    set_source_port,
    set_source_volume_pct,
    split_device,
    to_pulse_path,
)

DAY_NAMES = ["Lun", "Mar", "Mer", "Gio", "Ven", "Sab", "Dom"]
# 60 px/ora = 1 px/minuto → durata proporzionale a colpo d'occhio
PX_PER_HOUR = 100
DAY_HEIGHT = 24 * PX_PER_HOUR
COL_WIDTH = 150
HEADER_H = 36
TIME_GUTTER = 56

# Sentinel clip_id del player per la playlist filler (non è un clip del DB)
ANTI_BIANCO_CLIP_ID = -1
SETTING_ANTI_BIANCO = "anti_bianco_playlist"
# Retry stream LINK quando è offline e siamo in failover ANTI BIANCO
LINK_RETRY_SEC = 5.0

# Silence-gate: default (override da SQLite settings / SETTING)
SILENCE_THRESH_DB_DEFAULT = -40.0
SILENCE_HOLD_SEC_DEFAULT = 8.0
SILENCE_RECOVER_SEC_DEFAULT = 2.5
SILENCE_HYST_DB = 5.0  # ripresa = soglia_innesco + isteresi (fissa)
SILENCE_GRACE_SEC = 3.0  # dopo ripresa, non rivalutare subito

# Prefissi settings SQLite per tipo sorgente
SILENCE_KIND_FILE = "file"  # file + playlist
SILENCE_KIND_LINK = "link"
SILENCE_KIND_LIVE = "live"

# ASPETTO
SETTING_CLIP_FONT_PT = "aspect_clip_font_pt"
CLIP_FONT_PT_DEFAULT = 9
SETTING_ZOOM_PX_HOUR = "aspect_zoom_px_per_hour"
ZOOM_PX_MIN = 30
ZOOM_PX_MAX = 400
ZOOM_PX_STEP = 10

# VU: 0 dBFS = fondo scala; verde / giallo (−4) / rosso (−1)
VU_FLOOR_DB = -60.0
VU_ZERO_FRAC = 1.0
VU_YELLOW_DB = -4.0
VU_RED_DB = -1.0


def monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def silence_setting_keys(kind: str) -> tuple[str, str, str]:
    """Chiavi SQLite: hold_sec, recover_sec, thresh_db per kind file|link|live."""
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


def day_fraction(dt: datetime) -> float:
    sod = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return (dt - sod).total_seconds() / 86400.0


def db_to_meter_frac(db: float) -> float:
    """Mappa dBFS → frazione barra (0 dBFS = pieno)."""
    if db <= VU_FLOOR_DB:
        return 0.0
    if db >= 0.0:
        return 1.0
    return (db - VU_FLOOR_DB) / (0.0 - VU_FLOOR_DB)


class VUMeter(QWidget):
    """Barra VU: 0 dBFS a fondo scala; verde / giallo (−4) / rosso (−1)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._db = -120.0
        self.setMinimumWidth(160)
        self.setFixedHeight(22)
        self.setToolTip(
            "VU: 0 dBFS = fondo scala · giallo da −4 dB · rosso da −1 dB"
        )

    def set_level(self, level: float) -> None:
        """Compat: livello lineare 0..1 → dB approssimato."""
        level = max(0.0, min(1.0, float(level)))
        if level <= 0.0:
            self.set_db(-120.0)
        else:
            self.set_db(VU_FLOOR_DB + (0.0 - VU_FLOOR_DB) * level)

    def set_db(self, db: float) -> None:
        self._db = float(db)
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(40, 44, 48))
        w = self.width()
        h = self.height()
        fill_w = int(w * db_to_meter_frac(self._db))
        yellow_x = int(w * db_to_meter_frac(VU_YELLOW_DB))
        red_x = int(w * db_to_meter_frac(VU_RED_DB))
        if fill_w > 0:
            # verde fino a −4 dB
            g_end = min(fill_w, yellow_x)
            if g_end > 0:
                p.fillRect(0, 0, g_end, h, QColor(80, 180, 90))
            # giallo −4 … −1
            if fill_w > yellow_x:
                y_end = min(fill_w, red_x)
                if y_end > yellow_x:
                    p.fillRect(
                        yellow_x, 0, y_end - yellow_x, h, QColor(220, 180, 50)
                    )
            # rosso da −1 dB
            if fill_w > red_x:
                p.fillRect(red_x, 0, fill_w - red_x, h, QColor(220, 70, 60))
        # tacca −4 dB (inizio giallo)
        p.setPen(QPen(QColor(230, 230, 230), 1))
        p.drawLine(yellow_x, 0, yellow_x, h - 1)
        p.setPen(QColor(90, 95, 100))
        p.drawRect(0, 0, w - 1, h - 1)


class WeekTimeline(QWidget):
    clipActivated = pyqtSignal(int)
    dayDoubleClicked = pyqtSignal(object)  # date
    addAfterRequested = pyqtSignal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._week_monday = monday_of(date.today())
        self._clips: list[Clip] = []
        self._now = datetime.now()
        self._playing_id: int | None = None
        self._selected_id: int | None = None
        self._gutter = TIME_GUTTER
        self._col_w = COL_WIDTH
        self._clip_font_pt = CLIP_FONT_PT_DEFAULT
        self._px_per_hour = PX_PER_HOUR
        self._day_h = 24 * self._px_per_hour
        self.setMouseTracking(True)
        self._apply_size()

    def set_clip_font_pt(self, pt: int) -> None:
        """Dimensione carattere testo dentro i blocchi schedulazione."""
        self._clip_font_pt = max(6, min(24, int(pt)))
        self.update()

    def set_px_per_hour(self, px: int) -> None:
        """Zoom verticale: pixel per ora (allunga/accorcia la giornata)."""
        self._px_per_hour = max(ZOOM_PX_MIN, min(ZOOM_PX_MAX, int(px)))
        self._day_h = 24 * self._px_per_hour
        self._apply_size()
        self.update()

    def px_per_hour(self) -> int:
        return self._px_per_hour

    def day_height(self) -> int:
        return self._day_h

    def set_metrics(self, gutter: int, col_width: int) -> None:
        """Allinea colonne al header (stesse larghezze)."""
        self._gutter = max(40, int(gutter))
        self._col_w = max(80, int(col_width))
        self._apply_size()
        self.update()

    def _apply_size(self) -> None:
        w = self._gutter + 7 * self._col_w
        self.setFixedSize(w, self._day_h)

    def set_week(self, monday: date) -> None:
        self._week_monday = monday_of(monday)
        self.update()

    def week_monday(self) -> date:
        return self._week_monday

    def set_clips(self, clips: list[Clip]) -> None:
        self._clips = clips
        self.update()

    def set_now(self, now: datetime) -> None:
        self._now = now
        self.update()

    def set_playing_id(self, clip_id: int | None) -> None:
        self._playing_id = clip_id
        self.update()

    def set_selected_id(self, clip_id: int | None) -> None:
        self._selected_id = clip_id
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(self._gutter + 7 * self._col_w, self._day_h)

    def _day_dates(self) -> list[date]:
        return [self._week_monday + timedelta(days=i) for i in range(7)]

    def _y_for_fraction(self, frac: float) -> int:
        return int(frac * self._day_h)

    def _segments_for_clip(self, clip: Clip) -> list[tuple[date, float, float]]:
        """Lista (giorno, frac_start, frac_end) per pezzi nel range settimana."""
        segs: list[tuple[date, float, float]] = []
        week_start = datetime.combine(self._week_monday, datetime.min.time())
        week_end = week_start + timedelta(days=7)
        cur = max(clip.start_ts, week_start)
        end = min(clip.end_ts, week_end)
        if cur >= end:
            return segs
        while cur < end:
            day = cur.date()
            day_end = datetime.combine(day + timedelta(days=1), datetime.min.time())
            seg_end = min(end, day_end)
            segs.append((day, day_fraction(cur), day_fraction(seg_end)))
            cur = seg_end
        return segs

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(28, 30, 34))
        gutter = self._gutter
        col_w = self._col_w
        grid_right = gutter + 7 * col_w

        # Gutter ore
        p.setPen(QColor(160, 165, 170))
        font = QFont(self.font())
        font.setPointSize(8)
        p.setFont(font)
        for h in range(25):
            y = self._y_for_fraction(h / 24.0)
            p.setPen(QPen(QColor(55, 58, 64), 1))
            # Ultima riga (24:00) a filo inferiore widget
            if h == 24:
                y = min(y, self._day_h - 1)
            p.drawLine(gutter, y, grid_right, y)
            p.setPen(QColor(150, 155, 160))
            if h < 24:
                p.drawText(4, y + 12, f"{h:02d}:00")
            else:
                # Etichetta in basso senza uscire dal widget
                p.drawText(4, y - 2, "24:00")

        days = self._day_dates()
        today = self._now.date()

        for i, d in enumerate(days):
            x = gutter + i * col_w
            bg = QColor(34, 37, 42) if d != today else QColor(40, 48, 58)
            p.fillRect(x, 0, col_w - 1, self._day_h, bg)
            p.setPen(QColor(60, 64, 70))
            p.drawLine(x + col_w - 1, 0, x + col_w - 1, self._day_h)

        # Clip blocks
        for clip in self._clips:
            for day, f0, f1 in self._segments_for_clip(clip):
                if day not in days:
                    continue
                i = days.index(day)
                x = self._gutter + i * self._col_w + 2
                y0 = self._y_for_fraction(f0)
                y1 = max(y0 + 4, self._y_for_fraction(f1))
                rect = QRect(x, y0, self._col_w - 6, y1 - y0)

                past = clip.end_ts <= self._now
                playing = clip.id == self._playing_id
                selected = clip.id == self._selected_id

                fill = QColor(clip.fill_color)
                if past:
                    fill = QColor(
                        max(40, fill.red() // 2),
                        max(40, fill.green() // 2),
                        max(40, fill.blue() // 2),
                    )
                p.fillRect(rect, fill)
                if playing:
                    p.setPen(QPen(QColor(255, 255, 255), 2))
                elif selected:
                    p.setPen(QPen(QColor(255, 220, 120), 2))
                else:
                    p.setPen(QColor(20, 22, 24))
                p.drawRect(rect)
                # Testo: contrasto sul colore di sfondo
                lum = (
                    0.299 * fill.red() + 0.587 * fill.green() + 0.114 * fill.blue()
                )
                p.setPen(
                    QColor(20, 20, 20) if lum > 140 else QColor(245, 245, 245)
                )
                clip_font = QFont(self.font())
                clip_font.setPointSize(self._clip_font_pt)
                p.setFont(clip_font)
                # Riga 1 titolo — riga 2 file/LIVE — riga 3 orari
                title = clip.show_title
                if len(title) > 22:
                    title = title[:21] + "…"
                source = clip.source_label
                if len(source) > 22:
                    source = source[:21] + "…"
                t0 = clip.start_ts.strftime("%H:%M:%S")
                t1 = clip.end_ts.strftime("%H:%M:%S")
                label = f"{title}\n{source}\ndalle {t0} alle {t1}"
                p.drawText(
                    rect.adjusted(3, 2, -3, -2),
                    Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft,
                    label,
                )

        # Now line
        if self._week_monday <= today <= self._week_monday + timedelta(days=6):
            i = (today - self._week_monday).days
            x0 = self._gutter + i * self._col_w
            y = self._y_for_fraction(day_fraction(self._now))
            p.setPen(QPen(QColor(220, 80, 70), 2))
            p.drawLine(x0, y, x0 + self._col_w - 1, y)

    def _clip_at(self, pos: QPoint) -> Clip | None:
        if pos.x() < self._gutter:
            return None
        i = (pos.x() - self._gutter) // self._col_w
        if i < 0 or i > 6:
            return None
        day = self._week_monday + timedelta(days=i)
        frac = pos.y() / max(1, self._day_h)
        sod = datetime.combine(day, datetime.min.time())
        t = sod + timedelta(seconds=frac * 86400.0)
        for clip in self._clips:
            if clip.start_ts <= t < clip.end_ts:
                return clip
        return None

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            clip = self._clip_at(event.position().toPoint())
            self._selected_id = clip.id if clip else None
            self.update()
            if clip:
                self.clipActivated.emit(clip.id)
        elif event.button() == Qt.MouseButton.RightButton:
            clip = self._clip_at(event.position().toPoint())
            if clip:
                self._selected_id = clip.id
                self.update()
                self.addAfterRequested.emit(clip.id)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        # Click singolo già apre il dettaglio; doppio-click solo su giorno vuoto → aggiungi
        if self._clip_at(event.position().toPoint()):
            return
        if event.position().x() < self._gutter:
            return
        i = int((event.position().x() - self._gutter) // self._col_w)
        if 0 <= i <= 6:
            self.dayDoubleClicked.emit(self._week_monday + timedelta(days=i))


class AddChoiceDialog(QDialog):
    """Scegli se inserire file, LIVE, LINK o PLAYLIST."""

    CHOICE_FILE = 1
    CHOICE_LIVE = 2
    CHOICE_LINK = 3
    CHOICE_PLAYLIST = 4

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Aggiungi")
        self.setMinimumWidth(320)
        self.choice: int | None = None
        root = QVBoxLayout(self)
        title = QLabel("Scegli cosa inserire")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        font = title.font()
        font.setPointSize(max(11, font.pointSize() + 2))
        font.setBold(True)
        title.setFont(font)
        root.addWidget(title)
        root.addSpacing(8)

        btn_file = QPushButton("File audio…")
        btn_live = QPushButton("LIVE (ingresso)…")
        btn_link = QPushButton("LINK (stream http/https)…")
        btn_pl = QPushButton("PLAYLIST (m3u/pls)…")
        for b in (btn_file, btn_live, btn_link, btn_pl):
            b.setMinimumHeight(36)
            root.addWidget(b)
        btn_file.clicked.connect(lambda: self._pick(self.CHOICE_FILE))
        btn_live.clicked.connect(lambda: self._pick(self.CHOICE_LIVE))
        btn_link.clicked.connect(lambda: self._pick(self.CHOICE_LINK))
        btn_pl.clicked.connect(lambda: self._pick(self.CHOICE_PLAYLIST))

        root.addSpacing(6)
        cancel = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        cancel.rejected.connect(self.reject)
        root.addWidget(cancel)

    def _pick(self, choice: int) -> None:
        self.choice = choice
        self.accept()


class AntiBiancoDialog(QDialog):
    """Scegli (o disattiva) la playlist filler quando non c'è nulla di schedulato."""

    def __init__(self, current_path: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("ANTI BIANCO")
        self.setMinimumWidth(420)
        self.path = (current_path or "").strip()
        root = QVBoxLayout(self)
        info = QLabel(
            "Playlist in play quando non c'è nulla di schedulato,\n"
            "finché non parte qualcosa di schedulato."
        )
        info.setWordWrap(True)
        root.addWidget(info)
        self._lab = QLabel("")
        self._lab.setWordWrap(True)
        self._lab.setStyleSheet("color: #aaa;")
        root.addWidget(self._lab)
        self._refresh_label()

        row = QHBoxLayout()
        btn_pick = QPushButton("Scegli playlist…")
        btn_pick.clicked.connect(self._pick)
        btn_clear = QPushButton("Disattiva")
        btn_clear.clicked.connect(self._clear)
        row.addWidget(btn_pick)
        row.addWidget(btn_clear)
        root.addLayout(row)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(self.accept)
        root.addWidget(buttons)

    def _refresh_label(self) -> None:
        if self.path:
            self._lab.setText(f"Attuale: {self.path}")
        else:
            self._lab.setText("Attuale: (disattivato)")

    def _pick(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Scegli playlist ANTI BIANCO",
            str(Path(self.path).parent if self.path else Path.home()),
            "Playlist (*.m3u *.m3u8 *.pls);;Tutti (*)",
        )
        if not path:
            return
        self.path = path
        self._refresh_label()

    def _clear(self) -> None:
        self.path = ""
        self._refresh_label()


class SettingsDialog(QDialog):
    """Impostazioni app: ANTI BIANCO (silence-gate) e ASPETTO."""

    PAGE_ANTI = 0
    PAGE_ASPETTO = 1

    def __init__(self, db: PalinsestoDB, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.db = db
        self.setWindowTitle("SETTING")
        self.setMinimumSize(520, 520)

        root = QVBoxLayout(self)

        nav = QHBoxLayout()
        self.btn_anti = QPushButton("ANTI BIANCO")
        self.btn_aspetto = QPushButton("ASPETTO")
        for b in (self.btn_anti, self.btn_aspetto):
            b.setCheckable(True)
            b.setMinimumHeight(32)
            nav.addWidget(b)
        nav.addStretch(1)
        root.addLayout(nav)

        self._stack = QStackedWidget()
        self._page_anti = QWidget()
        self._page_aspetto = QWidget()
        self._anti_widgets: dict[str, dict[str, QDoubleSpinBox]] = {}
        self._font_spin: QSpinBox | None = None
        self._build_page_anti()
        self._build_page_aspetto()
        self._stack.addWidget(self._page_anti)
        self._stack.addWidget(self._page_aspetto)
        root.addWidget(self._stack, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self.btn_anti.clicked.connect(lambda: self._show_page(self.PAGE_ANTI))
        self.btn_aspetto.clicked.connect(lambda: self._show_page(self.PAGE_ASPETTO))
        self._show_page(self.PAGE_ANTI)

    def _show_page(self, idx: int) -> None:
        self._stack.setCurrentIndex(idx)
        self.btn_anti.setChecked(idx == self.PAGE_ANTI)
        self.btn_aspetto.setChecked(idx == self.PAGE_ASPETTO)

    def _make_silence_group(
        self, title: str, kind: str
    ) -> tuple[QGroupBox, dict[str, QDoubleSpinBox]]:
        """Gruppo: secondi innesco, secondi disinnesco, soglia dB."""
        params = load_silence_params(self.db, kind)
        box = QGroupBox(title)
        form = QFormLayout(box)

        sp_hold = QDoubleSpinBox()
        sp_hold.setRange(0.5, 120.0)
        sp_hold.setDecimals(1)
        sp_hold.setSingleStep(0.5)
        sp_hold.setSuffix(" s")
        sp_hold.setValue(params["hold_sec"])
        sp_hold.setToolTip("Secondi sotto soglia prima di attivare ANTI BIANCO")

        sp_rec = QDoubleSpinBox()
        sp_rec.setRange(0.5, 120.0)
        sp_rec.setDecimals(1)
        sp_rec.setSingleStep(0.5)
        sp_rec.setSuffix(" s")
        sp_rec.setValue(params["recover_sec"])
        sp_rec.setToolTip("Secondi sopra soglia (+isteresi) prima di riprendere la sorgente")

        sp_thr = QDoubleSpinBox()
        sp_thr.setRange(-80.0, 0.0)
        sp_thr.setDecimals(1)
        sp_thr.setSingleStep(1.0)
        sp_thr.setSuffix(" dBFS")
        sp_thr.setValue(params["thresh_db"])
        sp_thr.setToolTip(
            "Soglia innesco audio: sotto questo livello per N secondi → filler.\n"
            f"Ripresa automatica a soglia + {SILENCE_HYST_DB:.0f} dB."
        )

        form.addRow("Innesco (secondi):", sp_hold)
        form.addRow("Disinnesco (secondi):", sp_rec)
        form.addRow("Soglia dB innesco:", sp_thr)
        return box, {"hold": sp_hold, "recover": sp_rec, "thresh": sp_thr}

    def _build_page_anti(self) -> None:
        lay = QVBoxLayout(self._page_anti)
        title = QLabel("ANTI BIANCO — silence-gate audio")
        font = title.font()
        font.setBold(True)
        title.setFont(font)
        lay.addWidget(title)
        hint = QLabel(
            "Solo per silenzio audio sulla sorgente in onda.\n"
            "Buchi di palinsesto e LINK offline restano c’è/non c’è (come ora)."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #888;")
        lay.addWidget(hint)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        host = QWidget()
        host_lay = QVBoxLayout(host)

        for kind, label in (
            (SILENCE_KIND_FILE, "FILE / PLAYLIST"),
            (SILENCE_KIND_LINK, "LINK"),
            (SILENCE_KIND_LIVE, "LIVE"),
        ):
            box, widgets = self._make_silence_group(label, kind)
            self._anti_widgets[kind] = widgets
            host_lay.addWidget(box)

        host_lay.addStretch(1)
        scroll.setWidget(host)
        lay.addWidget(scroll, 1)

    def _build_page_aspetto(self) -> None:
        lay = QVBoxLayout(self._page_aspetto)
        title = QLabel("ASPETTO")
        font = title.font()
        font.setBold(True)
        title.setFont(font)
        lay.addWidget(title)
        hint = QLabel(
            "Aspetto della timeline di schedulazione.\n"
            "Salvataggio su SQLite (settings)."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #888;")
        lay.addWidget(hint)

        form = QFormLayout()
        self._font_spin = QSpinBox()
        self._font_spin.setRange(6, 24)
        self._font_spin.setSuffix(" pt")
        cur = int(
            round(
                self.db.get_float(SETTING_CLIP_FONT_PT, float(CLIP_FONT_PT_DEFAULT))
            )
        )
        self._font_spin.setValue(max(6, min(24, cur)))
        self._font_spin.setToolTip(
            "Dimensione del testo dentro i blocchi della schedulazione "
            "(titolo, sorgente, orari)."
        )
        form.addRow("Dimensioni carattere:", self._font_spin)
        lay.addLayout(form)
        lay.addStretch(1)

    def _on_accept(self) -> None:
        for kind, w in self._anti_widgets.items():
            save_silence_params(
                self.db,
                kind,
                hold_sec=w["hold"].value(),
                recover_sec=w["recover"].value(),
                thresh_db=w["thresh"].value(),
            )
        if self._font_spin is not None:
            self.db.set_float(SETTING_CLIP_FONT_PT, float(self._font_spin.value()))
        self.accept()


class StreamPreview(QWidget):
    """Volume + VU anteprima per URL stream (StreamLevelTap)."""

    def __init__(
        self, initial_pct: int = 100, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._tap: StreamLevelTap | None = None
        self._url = ""
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(4)

        vol_row = QHBoxLayout()
        vol_row.addWidget(QLabel("Volume stream:"))
        self.vol_slider = QSlider(Qt.Orientation.Horizontal)
        self.vol_slider.setRange(0, 150)
        self.vol_slider.setValue(max(0, min(150, int(initial_pct))))
        self.vol_slider.valueChanged.connect(self._on_vol)
        self.vol_label = QLabel(f"{self.vol_slider.value()}%")
        self.vol_label.setFixedWidth(44)
        vol_row.addWidget(self.vol_slider, 1)
        vol_row.addWidget(self.vol_label)
        root.addLayout(vol_row)

        vu_row = QHBoxLayout()
        vu_row.addWidget(QLabel("VU:"))
        self.vu = VUMeter()
        vu_row.addWidget(self.vu, 1)
        self.db_lab = QLabel("— dB")
        self.db_lab.setFixedWidth(56)
        vu_row.addWidget(self.db_lab)
        root.addLayout(vu_row)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.timeout.connect(self._restart_tap)
        self._pump = QTimer(self)
        self._pump.timeout.connect(lambda: pump_glib_once())

    def peak_gain(self) -> float:
        return max(0.0, self.vol_slider.value() / 100.0)

    def set_url(self, url: str) -> None:
        self._url = (url or "").strip()
        self._debounce.start(500)

    def _on_vol(self, value: int) -> None:
        self.vol_label.setText(f"{value}%")
        if self._tap is not None:
            self._tap.set_volume(value / 100.0)

    def _set_db(self, db: float) -> None:
        self.vu.set_db(db)
        self.db_lab.setText("— dB" if db <= -90 else f"{db:+.1f} dB")

    def _stop_tap(self) -> None:
        if self._tap is not None:
            try:
                self._tap.stop()
            except Exception:
                pass
            self._tap = None
        self._set_db(-120.0)

    def _restart_tap(self) -> None:
        self._stop_tap()
        if not self.isVisible() or not gstreamer_available():
            return
        try:
            url = normalize_stream_url(self._url)
        except ValueError:
            return
        try:
            self._tap = StreamLevelTap(
                url,
                on_db=self._set_db,
                volume=self.peak_gain(),
            )
        except (RuntimeError, ValueError):
            self.db_lab.setText("n/d")

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._pump.start(40)
        if self._url:
            self._debounce.start(200)

    def hideEvent(self, event) -> None:  # noqa: N802
        self._pump.stop()
        self._debounce.stop()
        self._stop_tap()
        super().hideEvent(event)

    def shutdown(self) -> None:
        self._pump.stop()
        self._debounce.stop()
        self._stop_tap()


class TimePickDialog(QDialog):
    def __init__(
        self,
        title: str,
        day: date,
        initial: datetime,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self._day = day
        layout = QFormLayout(self)
        self.time_edit = QTimeEdit()
        self.time_edit.setDisplayFormat("HH:mm:ss")
        self.time_edit.setTime(
            QTime(initial.hour, initial.minute, initial.second)
        )
        layout.addRow("Orario inizio:", self.time_edit)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def selected_datetime(self) -> datetime:
        t = self.time_edit.time()
        return datetime(
            self._day.year,
            self._day.month,
            self._day.day,
            t.hour(),
            t.minute(),
            t.second(),
        )


def _fill_source_combo(combo: QComboBox, selected: str = "default") -> None:
    """Popola combo: ingressi hardware (porte) prima, poi monitor uscite."""
    selected = normalize_device(selected)
    combo.blockSignals(True)
    combo.clear()
    combo.addItem("Ingresso predefinito del sistema", "default")
    sources = list_pulse_sources(include_monitors=True)
    hw = [s for s in sources if not s.is_monitor]
    mon = [s for s in sources if s.is_monitor]
    found = False
    if hw:
        combo.insertSeparator(combo.count())
        for src in hw:
            combo.addItem(src.label, src.name)
            if src.name == selected:
                found = True
    if mon:
        combo.insertSeparator(combo.count())
        for src in mon:
            combo.addItem(src.label, src.name)
            if src.name == selected:
                found = True
    if selected not in ("", "default") and not found:
        # compat: path salvato senza @port → seleziona porta attiva della source
        sel_base, sel_port = split_device(selected)
        for src in hw:
            if src.source_name != sel_base:
                continue
            if sel_port is None and src.port_active:
                selected = src.name
                found = True
                break
            if sel_port is not None and src.port == sel_port:
                selected = src.name
                found = True
                break
        if not found:
            combo.addItem(f"(salvato) {selected}", selected)
    idx = 0
    if selected and selected != "default":
        for i in range(combo.count()):
            if combo.itemData(i) == selected:
                idx = i
                break
    combo.setCurrentIndex(idx)
    combo.blockSignals(False)


class ColorPickerButton(QPushButton):
    """Pulsante colore trasmissione (salvato nel DB)."""

    def __init__(self, color: str = "#3D7AB5", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._color = normalize_color(color)
        self.setFixedHeight(28)
        self.setMinimumWidth(120)
        self.clicked.connect(self._pick)
        self._apply_style()

    def _apply_style(self) -> None:
        self.setText(self._color)
        self.setStyleSheet(
            f"background-color: {self._color}; color: #111; font-weight: bold;"
        )

    def _pick(self) -> None:
        chosen = QColorDialog.getColor(
            QColor(self._color), self, "Colore trasmissione"
        )
        if chosen.isValid():
            self._color = chosen.name().upper()
            self._apply_style()

    def color(self) -> str:
        return self._color

    def set_color(self, color: str) -> None:
        self._color = normalize_color(color)
        self._apply_style()


class SourcePicker(QWidget):
    """Gestione ingressi Pulse: elenco, volume, VU grezzo, unmute, default."""

    def __init__(
        self, selected: str = "default", parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)

        row = QHBoxLayout()
        self.combo = QComboBox()
        self.combo.setMinimumWidth(360)
        self.combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.combo.currentIndexChanged.connect(self._on_device_changed)
        btn = QPushButton("Aggiorna")
        btn.setToolTip("Rileggi gli ingressi Pulse (USB / scheda collegata)")
        btn.clicked.connect(lambda: self.refresh())
        row.addWidget(self.combo, 1)
        row.addWidget(btn)
        root.addLayout(row)

        vol_row = QHBoxLayout()
        vol_row.addWidget(QLabel("Volume ingresso:"))
        self.vol_slider = QSlider(Qt.Orientation.Horizontal)
        self.vol_slider.setRange(0, 150)
        self.vol_slider.setValue(100)
        self.vol_slider.setToolTip(
            "Volume source Pulse (0–150%). Mira vicino a 0 dBFS (fondo scala VU)."
        )
        self.vol_slider.valueChanged.connect(self._on_vol_changed)
        self.vol_label = QLabel("100%")
        self.vol_label.setFixedWidth(44)
        vol_row.addWidget(self.vol_slider, 1)
        vol_row.addWidget(self.vol_label)
        root.addLayout(vol_row)

        vu_row = QHBoxLayout()
        vu_row.addWidget(QLabel("VU ingresso:"))
        self.vu = VUMeter()
        self.vu.setMinimumWidth(180)
        vu_row.addWidget(self.vu, 1)
        self.db_lab = QLabel("— dB")
        self.db_lab.setFixedWidth(56)
        self.db_lab.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        vu_row.addWidget(self.db_lab)
        root.addLayout(vu_row)

        btn_row = QHBoxLayout()
        self.btn_unmute = QPushButton("Smuta ingresso")
        self.btn_unmute.setToolTip("Togli il mute Pulse sulla source selezionata")
        self.btn_unmute.clicked.connect(self._unmute)
        self.btn_default = QPushButton("Imposta come predefinito")
        self.btn_default.setToolTip("Imposta questa source come default di sistema")
        self.btn_default.clicked.connect(self._set_default)
        btn_row.addWidget(self.btn_unmute)
        btn_row.addWidget(self.btn_default)
        btn_row.addStretch(1)
        root.addLayout(btn_row)

        hint = QLabel(
            "Preferisci le voci «Ingresso: …» (microfono / linea / headset). "
            "«Monitor uscita» riprende ciò che esce dalle casse (loopback). "
            "VU = segnale grezzo (senza AGC LIVE)."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #888;")
        root.addWidget(hint)

        self._updating_vol = False
        self._tap: SourceLevelTap | None = None
        self._vu_timer = QTimer(self)
        self._vu_timer.timeout.connect(self._pump_vu)
        self.refresh(selected)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._restart_tap()
        self._vu_timer.start(40)

    def hideEvent(self, event) -> None:  # noqa: N802
        self._vu_timer.stop()
        self._stop_tap()
        super().hideEvent(event)

    def refresh(self, selected: str | None = None) -> None:
        cur = selected if isinstance(selected, str) else self.selected_device()
        _fill_source_combo(self.combo, cur)
        self._sync_volume_ui()
        if self.isVisible():
            self._restart_tap()

    def selected_device(self) -> str:
        data = self.combo.currentData()
        if data is None:
            return "default"
        return normalize_device(str(data))

    def selected_pulse_path(self) -> str:
        return to_pulse_path(self.selected_device())

    def _on_device_changed(self, _idx: int = 0) -> None:
        ensure_source_ready(self.selected_device())
        self._sync_volume_ui()
        if self.isVisible():
            self._restart_tap()

    def _sync_volume_ui(self) -> None:
        pct = get_source_volume_pct(self.selected_device())
        self._updating_vol = True
        self.vol_slider.setValue(pct)
        self.vol_label.setText(f"{pct}%")
        self._updating_vol = False

    def _on_vol_changed(self, value: int) -> None:
        self.vol_label.setText(f"{value}%")
        if self._updating_vol:
            return
        set_source_volume_pct(self.selected_device(), value)
        set_source_mute(self.selected_device(), False)

    def _unmute(self) -> None:
        set_source_mute(self.selected_device(), False)
        self.refresh()

    def _set_default(self) -> None:
        name = self.selected_device()
        if name == "default":
            return
        set_default_source(name)
        self.refresh(name)

    def _set_vu_db(self, db: float) -> None:
        self.vu.set_db(db)
        if db <= -90.0:
            self.db_lab.setText("— dB")
        else:
            self.db_lab.setText(f"{db:+.1f} dB")

    def _stop_tap(self) -> None:
        if self._tap is not None:
            try:
                self._tap.stop()
            except Exception:
                pass
            self._tap = None
        self._set_vu_db(-120.0)

    def _restart_tap(self) -> None:
        self._stop_tap()
        if not gstreamer_available():
            return
        try:
            device = ensure_source_ready(self.selected_device())
            if not device or device == "default":
                device = get_default_source()
            if not device or device == "default":
                return
            self._tap = SourceLevelTap(device, on_db=self._set_vu_db)
        except RuntimeError:
            self.db_lab.setText("n/d")

    def _pump_vu(self) -> None:
        pump_glib_once()


class LiveSlotDialog(QDialog):
    """Schedula una fascia ingresso audio → uscita (trasmissione live)."""

    def __init__(
        self,
        day: date,
        initial_start: datetime,
        parent: QWidget | None = None,
        default_device: str = "default",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Slot ingresso LIVE")
        self.setMinimumWidth(520)
        self._day = day
        layout = QFormLayout(self)
        self.title_edit = QLineEdit("Trasmissione live")
        layout.addRow("Nome trasmissione:", self.title_edit)
        self.desc_edit = QPlainTextEdit()
        self.desc_edit.setPlaceholderText("Descrizione (opzionale)")
        self.desc_edit.setFixedHeight(56)
        layout.addRow("Descrizione:", self.desc_edit)
        self.start_edit = QTimeEdit()
        self.start_edit.setDisplayFormat("HH:mm:ss")
        self.start_edit.setTime(
            QTime(
                initial_start.hour,
                initial_start.minute,
                initial_start.second,
            )
        )
        end0 = initial_start + timedelta(hours=1)
        if end0.date() != day:
            end0 = datetime.combine(day, datetime.max.time()).replace(microsecond=0)
        self.end_edit = QTimeEdit()
        self.end_edit.setDisplayFormat("HH:mm:ss")
        self.end_edit.setTime(QTime(end0.hour, end0.minute, end0.second))
        layout.addRow("Inizio:", self.start_edit)
        layout.addRow("Fine:", self.end_edit)
        self.source_picker = SourcePicker(default_device)
        layout.addRow("Ingresso audio:", self.source_picker)
        self.color_btn = ColorPickerButton(DEFAULT_LIVE_COLOR)
        layout.addRow("Colore:", self.color_btn)
        hint = QLabel(
            "In quella fascia l’ingresso scelto va in uscita, con normalize fisso a 0 dB.\n"
            "Volume e mute si gestiscono qui sopra (non serve pavucontrol)."
        )
        hint.setStyleSheet("color: #888;")
        layout.addRow(hint)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def _dt(self, edit: QTimeEdit) -> datetime:
        t = edit.time()
        return datetime(
            self._day.year,
            self._day.month,
            self._day.day,
            t.hour(),
            t.minute(),
            t.second(),
        )

    def values(self) -> tuple[datetime, datetime, str, str, str, str]:
        start = self._dt(self.start_edit)
        end = self._dt(self.end_edit)
        if end <= start:
            end = end + timedelta(days=1)
        title = self.title_edit.text().strip() or "Trasmissione live"
        description = self.desc_edit.toPlainText().strip()
        return (
            start,
            end,
            title,
            description,
            self.source_picker.selected_pulse_path(),
            self.color_btn.color(),
        )


class LinkSlotDialog(QDialog):
    """Schedula una fascia stream HTTP/HTTPS esterno."""

    def __init__(
        self,
        day: date,
        initial_start: datetime,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Slot LINK stream")
        self.setMinimumWidth(520)
        self._day = day
        layout = QFormLayout(self)
        self.title_edit = QLineEdit("Stream")
        layout.addRow("Nome trasmissione:", self.title_edit)
        self.desc_edit = QPlainTextEdit()
        self.desc_edit.setPlaceholderText("Descrizione (opzionale)")
        self.desc_edit.setFixedHeight(56)
        layout.addRow("Descrizione:", self.desc_edit)
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("https://esempio.org/stream.mp3")
        layout.addRow("URL stream:", self.url_edit)
        self.start_edit = QTimeEdit()
        self.start_edit.setDisplayFormat("HH:mm:ss")
        self.start_edit.setTime(
            QTime(
                initial_start.hour,
                initial_start.minute,
                initial_start.second,
            )
        )
        end0 = initial_start + timedelta(hours=1)
        if end0.date() != day:
            end0 = datetime.combine(day, datetime.max.time()).replace(microsecond=0)
        self.end_edit = QTimeEdit()
        self.end_edit.setDisplayFormat("HH:mm:ss")
        self.end_edit.setTime(QTime(end0.hour, end0.minute, end0.second))
        layout.addRow("Inizio:", self.start_edit)
        layout.addRow("Fine:", self.end_edit)
        self.stream_preview = StreamPreview(100)
        layout.addRow("Anteprima:", self.stream_preview)
        self.url_edit.textChanged.connect(self.stream_preview.set_url)
        self.color_btn = ColorPickerButton(DEFAULT_LINK_COLOR)
        layout.addRow("Colore:", self.color_btn)
        hint = QLabel(
            "Riproduce lo stream http/https nella fascia oraria scelta.\n"
            "Serve rete; formati tipici: mp3, aac, ogg, ice/shoutcast."
        )
        hint.setStyleSheet("color: #888;")
        layout.addRow(hint)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def done(self, r: int) -> None:  # noqa: N802
        self.stream_preview.shutdown()
        super().done(r)

    def _dt(self, edit: QTimeEdit) -> datetime:
        t = edit.time()
        return datetime(
            self._day.year,
            self._day.month,
            self._day.day,
            t.hour(),
            t.minute(),
            t.second(),
        )

    def values(self) -> tuple[datetime, datetime, str, str, str, str, float]:
        start = self._dt(self.start_edit)
        end = self._dt(self.end_edit)
        if end <= start:
            end = end + timedelta(days=1)
        url = normalize_stream_url(self.url_edit.text())
        title = self.title_edit.text().strip() or link_display_name(url)
        description = self.desc_edit.toPlainText().strip()
        return (
            start,
            end,
            title,
            description,
            url,
            self.color_btn.color(),
            self.stream_preview.peak_gain(),
        )


class ClipDetailDialog(QDialog):
    """Popup riepilogo trasmissione: OK / MODIFICA / ELIMINA."""

    ACTION_OK = 1
    ACTION_EDIT = 2
    ACTION_DELETE = 3

    def __init__(self, clip: Clip, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Dettaglio trasmissione")
        self.setMinimumWidth(440)
        self.action = self.ACTION_OK
        root = QVBoxLayout(self)

        color_bar = QLabel("")
        color_bar.setFixedHeight(8)
        color_bar.setStyleSheet(f"background-color: {clip.fill_color};")
        root.addWidget(color_bar)

        form = QFormLayout()
        form.addRow("Titolo:", QLabel(clip.show_title))
        desc = clip.description.strip() if clip.description else "—"
        desc_lab = QLabel(desc)
        desc_lab.setWordWrap(True)
        form.addRow("Descrizione:", desc_lab)

        if clip.is_live:
            form.addRow("Tipo:", QLabel("LIVE (ingresso audio)"))
            form.addRow("Ingresso:", QLabel(clip.path))
        elif clip.is_link:
            form.addRow("Tipo:", QLabel("LINK (stream HTTP/HTTPS)"))
            url_lab = QLabel(clip.path)
            url_lab.setWordWrap(True)
            url_lab.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            form.addRow("URL:", url_lab)
        elif clip.is_playlist:
            form.addRow("Tipo:", QLabel("PLAYLIST (m3u/pls)"))
            form.addRow("File:", QLabel(clip.source_label))
            path_lab = QLabel(clip.path)
            path_lab.setWordWrap(True)
            path_lab.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            form.addRow("Percorso:", path_lab)
        else:
            form.addRow("File:", QLabel(clip.source_label))
            path_lab = QLabel(clip.path)
            path_lab.setWordWrap(True)
            path_lab.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            form.addRow("Percorso:", path_lab)

        day = clip.start_ts
        day_name = DAY_NAMES[day.weekday()]
        form.addRow(
            "Giorno:",
            QLabel(f"{day_name} {day.strftime('%d/%m/%Y')}"),
        )
        form.addRow(
            "Orario:",
            QLabel(
                f"dalle {clip.start_ts.strftime('%H:%M:%S')} "
                f"alle {clip.end_ts.strftime('%H:%M:%S')}"
            ),
        )
        dur_m = max(1, round(clip.duration_ms / 60000))
        form.addRow("Durata:", QLabel(f"{dur_m} min circa"))
        root.addLayout(form)

        buttons = QHBoxLayout()
        btn_edit = QPushButton("MODIFICA")
        btn_del = QPushButton("ELIMINA")
        btn_ok = QPushButton("OK")
        btn_ok.setDefault(True)
        btn_edit.clicked.connect(self._on_edit)
        btn_del.clicked.connect(self._on_delete)
        btn_ok.clicked.connect(self._on_ok)
        buttons.addWidget(btn_edit)
        buttons.addWidget(btn_del)
        buttons.addStretch(1)
        buttons.addWidget(btn_ok)
        root.addLayout(buttons)

    def _on_ok(self) -> None:
        self.action = self.ACTION_OK
        self.accept()

    def _on_edit(self) -> None:
        self.action = self.ACTION_EDIT
        self.accept()

    def _on_delete(self) -> None:
        self.action = self.ACTION_DELETE
        self.accept()


class EditClipDialog(QDialog):
    """Modifica completa: orari, file/ingresso, titolo, descrizione, colore."""

    def __init__(self, clip: Clip, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Modifica trasmissione")
        self.setMinimumWidth(540)
        self._clip = clip
        self._path = clip.path
        self._display_name = clip.display_name
        self._duration_ms = clip.duration_ms
        self._peak_gain = clip.peak_gain
        layout = QFormLayout(self)

        if clip.is_live:
            kind = "LIVE (ingresso audio)"
        elif clip.is_link:
            kind = "LINK (stream HTTP/HTTPS)"
        elif clip.is_playlist:
            kind = "PLAYLIST (m3u/pls)"
        else:
            kind = "File audio"
        layout.addRow("Tipo:", QLabel(kind))

        self.date_edit = QDateEdit()
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("dd/MM/yyyy")
        self.date_edit.setDate(
            QDate(clip.start_ts.year, clip.start_ts.month, clip.start_ts.day)
        )
        layout.addRow("Giorno:", self.date_edit)

        self.start_edit = QTimeEdit()
        self.start_edit.setDisplayFormat("HH:mm:ss")
        self.start_edit.setTime(
            QTime(
                clip.start_ts.hour,
                clip.start_ts.minute,
                clip.start_ts.second,
            )
        )
        layout.addRow("Inizio:", self.start_edit)

        self.end_edit = QTimeEdit()
        self.end_edit.setDisplayFormat("HH:mm:ss")
        self.end_edit.setTime(
            QTime(clip.end_ts.hour, clip.end_ts.minute, clip.end_ts.second)
        )
        layout.addRow("Fine:", self.end_edit)

        self._file_label: QLabel | None = None
        self.source_picker: SourcePicker | None = None
        self.url_edit: QLineEdit | None = None
        self.stream_preview: StreamPreview | None = None
        if clip.is_live:
            self.source_picker = SourcePicker(clip.path)
            layout.addRow("Ingresso audio:", self.source_picker)
        elif clip.is_link:
            self.url_edit = QLineEdit(clip.path)
            self.url_edit.setPlaceholderText("https://…")
            layout.addRow("URL stream:", self.url_edit)
            pct = int(round(max(0.0, min(1.5, clip.peak_gain)) * 100))
            self.stream_preview = StreamPreview(pct)
            self.stream_preview.set_url(clip.path)
            layout.addRow("Anteprima:", self.stream_preview)
            self.url_edit.textChanged.connect(self.stream_preview.set_url)
            hint_link = QLabel(
                "Stream esterno http/https. La durata è quella della fascia oraria."
            )
            hint_link.setStyleSheet("color: #888;")
            layout.addRow(hint_link)
        elif clip.is_playlist:
            self._file_label = QLabel(clip.path)
            self._file_label.setWordWrap(True)
            self._file_label.setStyleSheet("color: #888;")
            layout.addRow("Playlist:", self._file_label)
            btn_file = QPushButton("Cambia playlist…")
            btn_file.clicked.connect(self._change_playlist)
            layout.addRow("", btn_file)
            self.start_edit.timeChanged.connect(self._sync_file_end)
            self.date_edit.dateChanged.connect(self._sync_file_end)
            hint_pl = QLabel(
                "La fine segue la durata totale dei brani (loop in onda fino a fine fascia)."
            )
            hint_pl.setStyleSheet("color: #888;")
            layout.addRow(hint_pl)
            self.end_edit.setEnabled(False)
        else:
            self._file_label = QLabel(clip.path)
            self._file_label.setWordWrap(True)
            self._file_label.setStyleSheet("color: #888;")
            layout.addRow("File:", self._file_label)
            btn_file = QPushButton("Cambia file…")
            btn_file.clicked.connect(self._change_file)
            layout.addRow("", btn_file)
            self.start_edit.timeChanged.connect(self._sync_file_end)
            self.date_edit.dateChanged.connect(self._sync_file_end)
            hint_file = QLabel(
                "La fine segue la durata del file (cambia file per aggiornarla)."
            )
            hint_file.setStyleSheet("color: #888;")
            layout.addRow(hint_file)
            self.end_edit.setEnabled(False)

        self.title_edit = QLineEdit(clip.show_title)
        layout.addRow("Nome trasmissione:", self.title_edit)
        self.desc_edit = QPlainTextEdit(clip.description or "")
        self.desc_edit.setFixedHeight(72)
        layout.addRow("Descrizione:", self.desc_edit)
        self.color_btn = ColorPickerButton(clip.fill_color)
        layout.addRow("Colore:", self.color_btn)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def done(self, r: int) -> None:  # noqa: N802
        if self.stream_preview is not None:
            self.stream_preview.shutdown()
        super().done(r)

    def _day(self) -> date:
        qd = self.date_edit.date()
        return date(qd.year(), qd.month(), qd.day())

    def _dt(self, edit: QTimeEdit) -> datetime:
        t = edit.time()
        d = self._day()
        return datetime(d.year, d.month, d.day, t.hour(), t.minute(), t.second())

    def _sync_file_end(self, *_args) -> None:
        if self._clip.is_live or self._clip.is_link:
            return
        start = self._dt(self.start_edit)
        end = start + timedelta(milliseconds=self._duration_ms)
        self.end_edit.blockSignals(True)
        self.end_edit.setTime(QTime(end.hour, end.minute, end.second))
        self.end_edit.blockSignals(False)

    def _change_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Scegli file audio",
            str(Path(self._path).parent if self._path else Path.home()),
            "Audio (*.wav *.mp3 *.flac *.ogg *.opus *.m4a *.aac *.wma *.aiff *.aif);;Tutti (*)",
        )
        if not path:
            return
        p = Path(path)
        try:
            duration_ms, peak_gain, title, description = probe_audio(p)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Probe", f"Impossibile analizzare il file:\n{exc}")
            return
        self._path = str(p)
        self._display_name = p.name
        self._duration_ms = duration_ms
        self._peak_gain = peak_gain
        if self._file_label is not None:
            self._file_label.setText(self._path)
        if not self.title_edit.text().strip() or self.title_edit.text() == self._clip.show_title:
            if title:
                self.title_edit.setText(title)
        if not self.desc_edit.toPlainText().strip() and description:
            self.desc_edit.setPlainText(description)
        self._sync_file_end()

    def _change_playlist(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Scegli playlist",
            str(Path(self._path).parent if self._path else Path.home()),
            "Playlist (*.m3u *.m3u8 *.pls);;Tutti (*)",
        )
        if not path:
            return
        p = Path(path)
        try:
            duration_ms, peak_gain, title, description, _tracks, _durs = probe_playlist(
                p
            )
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(
                self, "Playlist", f"Impossibile analizzare la playlist:\n{exc}"
            )
            return
        self._path = str(p)
        self._display_name = p.name
        self._duration_ms = duration_ms
        self._peak_gain = peak_gain
        if self._file_label is not None:
            self._file_label.setText(self._path)
        if not self.title_edit.text().strip() or self.title_edit.text() == self._clip.show_title:
            if title:
                self.title_edit.setText(title)
        if description:
            self.desc_edit.setPlainText(description)
        self._sync_file_end()

    def values(self) -> dict:
        start = self._dt(self.start_edit)
        end = self._dt(self.end_edit)
        if end <= start:
            end = end + timedelta(days=1)
        title = self.title_edit.text().strip() or "Senza titolo"
        description = self.desc_edit.toPlainText().strip()
        out: dict = {
            "title": title,
            "description": description,
            "color": self.color_btn.color(),
            "start": start,
            "end": end,
        }
        if self._clip.is_live:
            assert self.source_picker is not None
            out["device"] = self.source_picker.selected_pulse_path()
        elif self._clip.is_link:
            assert self.url_edit is not None
            assert self.stream_preview is not None
            url = normalize_stream_url(self.url_edit.text())
            out["path"] = url
            out["display_name"] = link_display_name(url)
            out["peak_gain"] = self.stream_preview.peak_gain()
        else:
            out["path"] = self._path
            out["display_name"] = self._display_name
            out["duration_ms"] = self._duration_ms
            out["peak_gain"] = self._peak_gain
        return out


class MixerRow(QWidget):
    """Una riga del mixer: nome, mute, Attiva porta, volume, VU."""

    def __init__(self, source: PulseSource, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.source = source
        self.source_name = source.name
        self._updating = False

        root = QVBoxLayout(self)
        root.setContentsMargins(4, 6, 4, 6)
        root.setSpacing(4)

        title = QLabel(source.label)
        title.setWordWrap(True)
        default_src = get_default_source()
        if source.source_name == default_src or source.name == default_src:
            title.setText(f"{source.label}  ★ predefinito")
        root.addWidget(title)

        row = QHBoxLayout()
        self.btn_mute = QPushButton("Mutato" if source.muted else "Mute")
        self.btn_mute.setCheckable(True)
        self.btn_mute.setChecked(source.muted)
        self.btn_mute.setFixedWidth(72)
        self.btn_mute.setToolTip("Mute / unmute di questa source Pulse")
        self.btn_mute.toggled.connect(self._on_mute)
        row.addWidget(self.btn_mute)

        self.btn_activate: QPushButton | None = None
        if source.port and not source.is_monitor and not source.port_active:
            self.btn_activate = QPushButton("Attiva")
            self.btn_activate.setFixedWidth(64)
            self.btn_activate.setToolTip(
                "Su HDA una sola porta ingresso alla volta — attiva questa"
            )
            self.btn_activate.clicked.connect(self._activate_port)
            row.addWidget(self.btn_activate)

        self.vol = QSlider(Qt.Orientation.Horizontal)
        self.vol.setRange(0, 150)
        self.vol.setValue(source.volume_pct)
        self.vol.setToolTip(
            "Volume ingresso Pulse (0–150%). Mira vicino a 0 dBFS (fondo scala)."
        )
        self.vol.valueChanged.connect(self._on_vol)
        row.addWidget(self.vol, 1)

        self.vol_lab = QLabel(f"{source.volume_pct}%")
        self.vol_lab.setFixedWidth(40)
        row.addWidget(self.vol_lab)

        self.vu = VUMeter()
        self.vu.setMinimumWidth(180)
        self.vu.setFixedWidth(200)
        row.addWidget(self.vu)

        self.db_lab = QLabel("— dB")
        self.db_lab.setFixedWidth(56)
        self.db_lab.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        row.addWidget(self.db_lab)
        root.addLayout(row)

        self._muted = source.muted

    def set_db(self, db: float) -> None:
        self.vu.set_db(db)
        if db <= -90.0:
            self.db_lab.setText("— dB")
        else:
            self.db_lab.setText(f"{db:+.1f} dB")

    def _activate_port(self) -> None:
        if not self.source.port:
            return
        set_source_port(self.source.source_name, self.source.port)
        parent = self.parent()
        while parent is not None and not isinstance(parent, MixerDialog):
            parent = parent.parent()
        if isinstance(parent, MixerDialog):
            parent.refresh()

    def _on_vol(self, value: int) -> None:
        self.vol_lab.setText(f"{value}%")
        if self._updating:
            return
        set_source_volume_pct(self.source_name, value)
        if self._muted:
            set_source_mute(self.source_name, False)
            self._muted = False
            self.btn_mute.blockSignals(True)
            self.btn_mute.setChecked(False)
            self.btn_mute.setText("Mute")
            self.btn_mute.blockSignals(False)

    def _on_mute(self, muted: bool) -> None:
        self._muted = muted
        set_source_mute(self.source_name, muted)
        self.btn_mute.setText("Mutato" if muted else "Mute")
        if muted:
            self.set_db(-120.0)

    def sync_from_pulse(self, source: PulseSource) -> None:
        self.source = source
        self._updating = True
        self.vol.setValue(source.volume_pct)
        self.vol_lab.setText(f"{source.volume_pct}%")
        self.btn_mute.setChecked(source.muted)
        self.btn_mute.setText("Mutato" if source.muted else "Mute")
        self._muted = source.muted
        self._updating = False


class MixerDialog(QDialog):
    """Popup mixer ingressi: volume + VU per regolare prima del normalize LIVE."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("MIXER ingressi")
        self.setMinimumSize(720, 420)
        self._taps: list[SourceLevelTap] = []
        self._rows: dict[str, MixerRow] = {}
        self._tapped_bases: set[str] = set()

        root = QVBoxLayout(self)
        hint = QLabel(
            "Regola ogni ingresso verso 0 dBFS (fondo scala VU). "
            "Verde → giallo da −4 dB → rosso da −1 dB. "
            "Su HDA usa «Attiva» per cambiare porta (una alla volta). "
            "Così il normalize LIVE interviene il meno possibile."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #888;")
        root.addWidget(hint)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self._host = QWidget()
        self._host_layout = QVBoxLayout(self._host)
        self._host_layout.setContentsMargins(4, 4, 4, 4)
        self._host_layout.addStretch(1)
        self.scroll.setWidget(self._host)
        root.addWidget(self.scroll, 1)

        btn_row = QHBoxLayout()
        btn_refresh = QPushButton("Aggiorna elenco")
        btn_refresh.clicked.connect(self.refresh)
        btn_close = QPushButton("Chiudi")
        btn_close.clicked.connect(self.accept)
        btn_row.addWidget(btn_refresh)
        btn_row.addStretch(1)
        btn_row.addWidget(btn_close)
        root.addLayout(btn_row)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(40)

        self.refresh()

    def refresh(self) -> None:
        self._stop_taps()
        while self._host_layout.count() > 1:
            item = self._host_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._rows.clear()
        self._tapped_bases.clear()

        sources = list_pulse_sources(include_monitors=True)
        if not sources:
            empty = QLabel("Nessun ingresso Pulse trovato (pactl / PulseAudio?).")
            self._host_layout.insertWidget(0, empty)
            return

        for src in sources:
            row = MixerRow(src)
            self._rows[src.name] = row
            self._host_layout.insertWidget(self._host_layout.count() - 1, row)
            # VU solo su porta attiva (o monitor); una tap per source base
            if not src.is_monitor and src.port and not src.port_active:
                continue
            base = src.source_name or split_device(src.name)[0]
            if base in self._tapped_bases:
                continue
            if not gstreamer_available():
                row.db_lab.setText("n/d")
                continue
            try:
                device = ensure_source_ready(src.name)
                tap = SourceLevelTap(device, on_db=row.set_db)
                self._taps.append(tap)
                self._tapped_bases.add(base)
            except RuntimeError:
                row.db_lab.setText("n/d")

    def _stop_taps(self) -> None:
        for tap in self._taps:
            try:
                tap.stop()
            except Exception:
                pass
        self._taps.clear()

    def _tick(self) -> None:
        pump_glib_once()

    def done(self, r: int) -> None:  # noqa: N802
        self._timer.stop()
        self._stop_taps()
        super().done(r)


class MainWindow(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Quelo-palinsesto-radio")

        self.db = PalinsestoDB()
        self._running = False
        self._player: AudioPlayer | None = None
        # Preferenze tutte nel DB (niente salva_sessione)
        self._master = max(0.0, min(1.0, self.db.get_float("master_volume", 0.85)))
        self._default_live_device = self.db.get_setting(
            "default_live_device", "pulse:default"
        )
        self._anti_bianco_path = self.db.get_setting(SETTING_ANTI_BIANCO, "").strip()
        # Ripresa filler: offset ms lungo la playlist (non ripartire da capo)
        self._anti_bianco_resume_ms: int = 0
        # Silence-gate per tipo (FILE/PLAYLIST, LINK, LIVE) da SQLite
        self._silence_cfg: dict[str, dict[str, float]] = {}
        self._reload_silence_settings()
        # Failover LINK offline → ANTI BIANCO (clip_id del LINK + prossima retry)
        self._link_failover_clip_id: int | None = None
        self._link_retry_at: float = 0.0
        # Failover silenzio (portante ok, audio sotto soglia)
        self._silence_failover_clip_id: int | None = None
        self._silence_since: float | None = None
        self._silence_recover_since: float | None = None
        self._silence_grace_until: float = 0.0
        self._silence_monitor_last_db: float = -120.0
        self._silence_recover_pending: bool = False
        self._silence_status_at: float = 0.0
        self._silence_tap: SourceLevelTap | StreamLevelTap | FileLevelTap | None = None
        self._silence_pl_tracks: list[Path] = []
        self._silence_pl_durs: list[int] = []
        self._silence_pl_idx: int = 0
        self._restore_geometry()

        if gstreamer_available():
            try:
                self._player = AudioPlayer(
                    on_level=self._on_level,
                    on_eos=self._on_eos,
                    on_error=self._on_player_error,
                )
                self._player.set_master_volume(self._master)
            except RuntimeError as exc:
                QMessageBox.warning(self, "Player", str(exc))

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # Frecce settimana (fuori dalle colonne, così non sfalsano i giorni)
        nav = QHBoxLayout()
        self.btn_prev = QPushButton("◀")
        self.btn_prev.setFixedWidth(36)
        self.btn_prev.clicked.connect(self._prev_week)
        self.btn_next = QPushButton("▶")
        self.btn_next.setFixedWidth(36)
        self.btn_next.clicked.connect(self._next_week)
        nav.addWidget(self.btn_prev)
        nav.addStretch(1)
        nav.addWidget(self.btn_next)
        root.addLayout(nav)

        # Intestazioni giorno: STESSE larghezze della timeline (gutter + 7 colonne)
        self._days_header = QWidget()
        days_lay = QHBoxLayout(self._days_header)
        days_lay.setContentsMargins(0, 0, 0, 0)
        days_lay.setSpacing(0)
        self._gutter_lab = QLabel("")
        self._gutter_lab.setFixedWidth(TIME_GUTTER)
        days_lay.addWidget(self._gutter_lab)
        self.day_labels: list[QLabel] = []
        for _ in range(7):
            lab = QLabel("")
            lab.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lab.setFixedWidth(COL_WIDTH)
            font = lab.font()
            font.setBold(True)
            lab.setFont(font)
            self.day_labels.append(lab)
            days_lay.addWidget(lab)
        root.addWidget(
            self._days_header, 0, Qt.AlignmentFlag.AlignLeft
        )

        self.timeline = WeekTimeline()
        self.timeline.dayDoubleClicked.connect(self._add_on_day)
        self.timeline.clipActivated.connect(self._on_clip_clicked)
        self.timeline.addAfterRequested.connect(self._add_after_clip)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(False)
        self.scroll.setWidget(self.timeline)
        self.scroll.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.scroll.viewport().installEventFilter(self)
        root.addWidget(self.scroll, 1)
        QTimer.singleShot(0, self._sync_column_widths)

        # Controls
        controls = QHBoxLayout()
        self.btn_start = QPushButton("Start")
        self.btn_start.clicked.connect(self._start)
        self.btn_stop = QPushButton("Stop")
        self.btn_stop.clicked.connect(self._stop)
        self.btn_add = QPushButton("AGGIUNGI")
        self.btn_add.setToolTip("Inserisci file audio, LIVE o stream LINK")
        self.btn_add.clicked.connect(self._add_choice_dialog)
        self.btn_anti = QPushButton("ANTI BIANCO")
        self.btn_anti.clicked.connect(self._anti_bianco_dialog)
        self._refresh_anti_bianco_btn()
        self.btn_mixer = QPushButton("MIXER")
        self.btn_mixer.setToolTip(
            "Regola volume e VU di ogni ingresso (0 dBFS = fondo scala)"
        )
        self.btn_mixer.clicked.connect(self._open_mixer)
        self.btn_settings = QPushButton("SETTING")
        self.btn_settings.setToolTip("Impostazioni ANTI BIANCO e ASPETTO")
        self.btn_settings.clicked.connect(self._open_settings)

        controls.addWidget(self.btn_start)
        controls.addWidget(self.btn_stop)
        controls.addSpacing(12)
        controls.addWidget(self.btn_add)
        controls.addWidget(self.btn_anti)
        controls.addWidget(self.btn_mixer)
        controls.addWidget(self.btn_settings)
        controls.addSpacing(10)
        controls.addWidget(QLabel("Zoom"))
        self.btn_zoom_out = QPushButton("−")
        self.btn_zoom_out.setFixedWidth(28)
        self.btn_zoom_out.setToolTip("Riduci altezza ore (zoom verticale −)")
        self.btn_zoom_out.clicked.connect(lambda: self._nudge_zoom(-ZOOM_PX_STEP))
        self.btn_zoom_in = QPushButton("+")
        self.btn_zoom_in.setFixedWidth(28)
        self.btn_zoom_in.setToolTip("Aumenta altezza ore (zoom verticale +)")
        self.btn_zoom_in.clicked.connect(lambda: self._nudge_zoom(ZOOM_PX_STEP))
        self.lbl_zoom = QLabel("")
        self.lbl_zoom.setMinimumWidth(36)
        self.lbl_zoom.setToolTip("Pixel per ora nella timeline")
        controls.addWidget(self.btn_zoom_out)
        controls.addWidget(self.lbl_zoom)
        controls.addWidget(self.btn_zoom_in)
        controls.addSpacing(16)
        controls.addWidget(QLabel("VU"))
        self.vu = VUMeter()
        controls.addWidget(self.vu)
        controls.addWidget(QLabel("Volume"))
        self.vol = QSlider(Qt.Orientation.Horizontal)
        self.vol.setRange(0, 100)
        self.vol.setValue(int(self._master * 100))
        self.vol.setFixedWidth(140)
        self.vol.valueChanged.connect(self._volume_changed)
        controls.addWidget(self.vol)
        controls.addStretch(1)
        self.clock = QLabel("")
        self.clock.setMinimumWidth(160)
        controls.addWidget(self.clock)
        root.addLayout(controls)
        self._apply_aspect_settings()

        self.status = QLabel(
            f"DB (palinsesto + preferenze): {self.db.path}"
        )
        self.status.setStyleSheet("color: #888;")
        root.addWidget(self.status)

        self._selected_id: int | None = None
        self._refresh_week_labels()
        self._reload_clips()

        self._timer = QTimer(self)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

        # Debounce salvataggio volume su DB
        self._vol_save_timer = QTimer(self)
        self._vol_save_timer.setSingleShot(True)
        self._vol_save_timer.setInterval(400)
        self._vol_save_timer.timeout.connect(self._persist_volume)

        # Scroll to "now" roughly
        QTimer.singleShot(100, self._scroll_to_now)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self.scroll.viewport() and event.type() == QEvent.Type.Resize:
            self._sync_column_widths()
        return super().eventFilter(obj, event)

    def _sync_column_widths(self) -> None:
        """Colonne header = colonne timeline = larghezza viewport (allineate)."""
        vw = self.scroll.viewport().width()
        if vw < 200:
            return
        col = max(80, (vw - TIME_GUTTER) // 7)
        total = TIME_GUTTER + 7 * col
        self._gutter_lab.setFixedWidth(TIME_GUTTER)
        for lab in self.day_labels:
            lab.setFixedWidth(col)
        self._days_header.setFixedWidth(total)
        self.timeline.set_metrics(TIME_GUTTER, col)

    def _restore_geometry(self) -> None:
        # Default: massimizzata (desktop intero, pannelli LXQt restano visibili).
        self._want_maximized = self.db.get_setting("window_maximized", "1") == "1"
        geo = self.db.get_setting("window_geometry", "")
        if geo:
            try:
                x, y, w, h = (int(p) for p in geo.split(","))
                if w >= 640 and h >= 400:
                    self.setGeometry(x, y, w, h)
                    return
            except ValueError:
                pass
        self.resize(1180, 720)

    def _persist_settings(self) -> None:
        maximized = self.isMaximized()
        self.db.set_setting("window_maximized", "1" if maximized else "0")
        # In massimizzata salva la geometria "normale" (per quando si ripristina).
        g = self.normalGeometry() if maximized else self.geometry()
        self.db.set_setting(
            "window_geometry",
            f"{g.x()},{g.y()},{g.width()},{g.height()}",
        )
        self.db.set_float("master_volume", self._master)
        self.db.set_setting("default_live_device", self._default_live_device)

    def _persist_volume(self) -> None:
        self.db.set_float("master_volume", self._master)

    def _scroll_to_now(self) -> None:
        self._sync_column_widths()
        y = int(day_fraction(datetime.now()) * self.timeline.day_height()) - 80
        self.scroll.verticalScrollBar().setValue(max(0, y))

    def _nudge_zoom(self, delta: int) -> None:
        px = self.timeline.px_per_hour() + int(delta)
        px = max(ZOOM_PX_MIN, min(ZOOM_PX_MAX, px))
        self._apply_zoom(px, persist=True)

    def _apply_zoom(self, px: int, *, persist: bool = False) -> None:
        """Zoom verticale timeline; opzionalmente salva in SQLite."""
        bar = self.scroll.verticalScrollBar()
        old_h = max(1, self.timeline.day_height())
        frac = bar.value() / old_h
        self.timeline.set_px_per_hour(px)
        self.lbl_zoom.setText(str(self.timeline.px_per_hour()))
        self._sync_column_widths()
        bar.setValue(int(frac * self.timeline.day_height()))
        self.btn_zoom_out.setEnabled(self.timeline.px_per_hour() > ZOOM_PX_MIN)
        self.btn_zoom_in.setEnabled(self.timeline.px_per_hour() < ZOOM_PX_MAX)
        if persist:
            self.db.set_setting(
                SETTING_ZOOM_PX_HOUR, str(self.timeline.px_per_hour())
            )

    def _refresh_week_labels(self) -> None:
        monday = self.timeline.week_monday()
        for i, lab in enumerate(self.day_labels):
            d = monday + timedelta(days=i)
            lab.setText(f"{DAY_NAMES[i]} {d.day:02d}/{d.month:02d}")

    def _week_range(self) -> tuple[datetime, datetime]:
        monday = self.timeline.week_monday()
        start = datetime.combine(monday, datetime.min.time())
        end = start + timedelta(days=7)
        return start, end

    def _reload_clips(self) -> None:
        start, end = self._week_range()
        # Include spill from previous day into Monday
        clips = self.db.list_clips(start - timedelta(days=1), end + timedelta(days=1))
        self.timeline.set_clips(clips)
        self.timeline.set_selected_id(self._selected_id)

    def _prev_week(self) -> None:
        m = self.timeline.week_monday() - timedelta(days=7)
        self.timeline.set_week(m)
        self._refresh_week_labels()
        self._reload_clips()

    def _next_week(self) -> None:
        m = self.timeline.week_monday() + timedelta(days=7)
        self.timeline.set_week(m)
        self._refresh_week_labels()
        self._reload_clips()

    def _select_clip(self, clip_id: int) -> None:
        self._selected_id = clip_id
        self.timeline.set_selected_id(clip_id)
        clip = self.db.get_clip(clip_id)
        if clip:
            kind = (
                "LIVE"
                if clip.is_live
                else (
                    "LINK"
                    if clip.is_link
                    else ("PLAYLIST" if clip.is_playlist else "file")
                )
            )
            desc = f" — {clip.description}" if clip.description else ""
            self.status.setText(
                f"Selezionato ({kind}): {clip.show_title}{desc}  "
                f"[{clip.source_label}]  "
                f"{clip.start_ts.strftime('%a %d/%m %H:%M:%S')}–"
                f"{clip.end_ts.strftime('%H:%M:%S')}  |  DB: {self.db.path}"
            )

    def _on_clip_clicked(self, clip_id: int) -> None:
        self._select_clip(clip_id)
        clip = self.db.get_clip(clip_id)
        if clip is None:
            return
        dlg = ClipDetailDialog(clip, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        if dlg.action == ClipDetailDialog.ACTION_EDIT:
            self._edit_clip(clip_id)
        elif dlg.action == ClipDetailDialog.ACTION_DELETE:
            self._selected_id = clip_id
            self._delete_selected()

    def _volume_changed(self, value: int) -> None:
        self._master = value / 100.0
        if self._player:
            self._player.set_master_volume(self._master)
        self._vol_save_timer.start()

    def _on_level(self, db: float) -> None:
        self.vu.set_db(db)
        self._silence_observe_program(float(db))

    def _on_eos(self) -> None:
        self.timeline.set_playing_id(None)
        if self._running:
            self._maybe_link_failover("stream terminato (EOS)")

    def _on_player_error(self, msg: str) -> None:
        self.timeline.set_playing_id(None)
        if self._running and self._maybe_link_failover(msg):
            return
        self.status.setText(f"Errore player: {msg}")

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

    def _reload_silence_settings(self) -> None:
        """Ricarica da SQLite i parametri silence-gate per FILE/LINK/LIVE."""
        self._silence_cfg = {
            SILENCE_KIND_FILE: load_silence_params(self.db, SILENCE_KIND_FILE),
            SILENCE_KIND_LINK: load_silence_params(self.db, SILENCE_KIND_LINK),
            SILENCE_KIND_LIVE: load_silence_params(self.db, SILENCE_KIND_LIVE),
        }

    def _apply_aspect_settings(self) -> None:
        """Applica ASPETTO da SQLite (font clip + zoom verticale)."""
        pt = int(
            round(
                self.db.get_float(SETTING_CLIP_FONT_PT, float(CLIP_FONT_PT_DEFAULT))
            )
        )
        self.timeline.set_clip_font_pt(pt)
        px = int(
            round(
                self.db.get_float(SETTING_ZOOM_PX_HOUR, float(PX_PER_HOUR))
            )
        )
        self._apply_zoom(px, persist=False)

    def _silence_kind_for_clip(self, clip: Clip) -> str:
        if clip.is_live:
            return SILENCE_KIND_LIVE
        if clip.is_link:
            return SILENCE_KIND_LINK
        return SILENCE_KIND_FILE  # file + playlist

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
        """Stima livello programma togliendo il master UI (evita falsi trigger)."""
        m = float(self._master)
        if m <= 0.001:
            return -120.0
        return float(vu_db) - 20.0 * math.log10(m)

    def _silence_observe_program(self, vu_db: float) -> None:
        """Conta silenzio mentre una sorgente schedulata è in onda."""
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
            # Mute intenzionale dal fader Quelo: non attivare il gate
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
        """Sorgente connessa ma silenziosa → ANTI BIANCO + monitor livello."""
        self._clear_link_failover()
        self._silence_failover_clip_id = clip.id
        self._silence_since = None
        self._silence_recover_since = None
        self._silence_recover_pending = False
        detail = f" — {reason}" if reason else ""
        self.status.setText(
            f"Silenzio → ANTI BIANCO{detail}  |  DB: {self.db.path}"
        )
        self._capture_anti_bianco_resume()
        # Stesso percorso per LIVE/file/LINK: ANTI BIANCO sul player principale
        # (in cassa) + tap di monitor sulla sorgente per la ripresa.
        if self._player is None or self._player.clip_id != ANTI_BIANCO_CLIP_ID:
            self._scheduler_anti_bianco(silence_failover=True)
        self._start_silence_monitor(clip)

    def _on_silence_monitor_db(self, db: float) -> None:
        """Callback livello sorgente monitorata (silence failover)."""
        self._silence_monitor_last_db = float(db)
        self._silence_evaluate_recover(float(db))

    def _silence_evaluate_recover(self, db: float) -> None:
        """Se il monitor resta sopra soglia abbastanza a lungo → ripresa."""
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
                QTimer.singleShot(0, self._try_silence_recover)
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
                    on_error=lambda m: self.status.setText(
                        f"Silenzio monitor LIVE: {m}  |  DB: {self.db.path}"
                    ),
                )
                return
            if clip.is_link:
                self._silence_tap = StreamLevelTap(
                    clip.path,
                    on_db=self._on_silence_monitor_db,
                    on_error=lambda m: self.status.setText(
                        f"Silenzio monitor LINK: {m}  |  DB: {self.db.path}"
                    ),
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
            # file
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
            self.status.setText(
                f"Silenzio: monitor sorgente fallito ({exc})  |  DB: {self.db.path}"
            )

    def _silence_monitor_playlist_advance(self) -> None:
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
        """Audio tornato sopra soglia → riprendi il clip schedulato."""
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
        # Forza il re-avvio del clip (clip_id player è ANTI BIANCO)
        try:
            if self._player.clip_id is not None:
                self._player.stop()
        except Exception:
            pass
        self.status.setText(
            f"Audio ripreso → {playing.show_title}  |  DB: {self.db.path}"
        )
        self._scheduler_step()

    def _maybe_link_failover(self, reason: str = "") -> bool:
        """Se la fascia corrente è LINK, passa ad ANTI BIANCO (failover)."""
        if not self._running or self._player is None:
            return False
        now = datetime.now().replace(microsecond=0)
        clip = self.db.find_playing(now)
        if clip is None or not clip.is_link:
            return False
        self._enter_link_failover(clip, reason)
        return True

    def _enter_link_failover(self, clip: Clip, reason: str = "") -> None:
        """LINK offline: filler + retry periodico dello stream (senza reset inutili)."""
        self._clear_silence_failover()
        self._link_failover_clip_id = clip.id
        self._link_retry_at = time.monotonic() + LINK_RETRY_SEC
        detail = f" — {reason}" if reason else ""
        self.status.setText(
            f"LINK offline → ANTI BIANCO{detail}  |  DB: {self.db.path}"
        )
        # Se il filler c'è già, non riavviarlo da capo
        if self._player is not None and self._player.clip_id == ANTI_BIANCO_CLIP_ID:
            return
        self._scheduler_anti_bianco(link_failover=True)

    @staticmethod
    def _probe_link_url(url: str, timeout: float = 2.5) -> bool:
        """True se l'URL risponde e consegna almeno un pezzo di body (stream vivo)."""
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
        """Salva dove era la playlist ANTI BIANCO prima di interromperla."""
        if self._player is None:
            return
        if self._player.clip_id != ANTI_BIANCO_CLIP_ID:
            return
        self._anti_bianco_resume_ms = self._player.playlist_position_ms()

    def _try_link_recover(self, clip: Clip) -> bool:
        """Riprova LINK: prima probe HTTP (ANTI BIANCO resta in play), poi switch."""
        assert self._player is not None
        self._link_retry_at = time.monotonic() + LINK_RETRY_SEC
        if not self._probe_link_url(clip.path):
            # Stream ancora giù: non toccare il player (niente restart playlist)
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
            self.status.setText(
                f"LINK ancora offline ({exc}) → ANTI BIANCO  |  DB: {self.db.path}"
            )
            self._scheduler_anti_bianco(link_failover=True)
            return False
        # Probe ok + pipeline avviata: esci dal failover.
        # Se GStreamer fallisce in async, on_error/on_eos riattivano il failover.
        self._clear_link_failover()
        self.timeline.set_playing_id(clip.id)
        self.status.setText(
            f"LINK ripreso: {clip.show_title}  "
            f"({clip.path})  |  DB: {self.db.path}"
        )
        return True

    def _start(self) -> None:
        if self._player is None:
            QMessageBox.warning(
                self,
                "Player",
                "GStreamer non disponibile (python3-gi / gir1.2-gstreamer).",
            )
            return
        self._running = True
        self._clear_link_failover()
        self._clear_silence_failover()
        self._silence_grace_until = 0.0
        self.btn_start.setEnabled(False)
        self.status.setText(f"In onda…  |  DB: {self.db.path}")
        self._scheduler_step()

    def _stop(self) -> None:
        self._running = False
        self._clear_link_failover()
        self._clear_silence_failover()
        if self._player:
            self._player.stop()
        self.timeline.set_playing_id(None)
        self.btn_start.setEnabled(True)
        self.vu.set_db(-120.0)
        self.status.setText(f"Stop  |  DB: {self.db.path}")

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

        # Failover LINK: resta su ANTI BIANCO e riprova lo stream ogni N s
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

        # Failover silenzio: filler + monitor; ripresa gestita dal tap
        if (
            self._silence_failover_clip_id is not None
            and clip.id == self._silence_failover_clip_id
        ):
            if self._player.clip_id != ANTI_BIANCO_CLIP_ID:
                self._scheduler_anti_bianco(silence_failover=True)
            if self._silence_tap is None:
                self._start_silence_monitor(clip)
            return

        # Fascia diversa / non più LINK → esci dal failover
        if self._link_failover_clip_id is not None:
            self._clear_link_failover()
        if (
            self._silence_failover_clip_id is not None
            and self._silence_failover_clip_id != clip.id
        ):
            self._clear_silence_failover()

        if self._player.clip_id == clip.id:
            return
        # Lasciamo ANTI BIANCO: memorizza posizione per ripresa successiva
        if self._player.clip_id == ANTI_BIANCO_CLIP_ID:
            self._capture_anti_bianco_resume()
        try:
            if clip.is_live:
                self._player.play_live(
                    clip_id=clip.id,
                    device_path=clip.path or LIVE_PATH_DEFAULT,
                    peak_gain=clip.peak_gain,
                )
                self.timeline.set_playing_id(clip.id)
                self.status.setText(
                    f"In onda LIVE: {clip.show_title}  "
                    f"(ingresso {clip.path})  |  DB: {self.db.path}"
                )
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
                self.timeline.set_playing_id(clip.id)
                self.status.setText(
                    f"In onda LINK: {clip.show_title}  "
                    f"({clip.path})  |  DB: {self.db.path}"
                )
                return
            if clip.is_playlist:
                pl = Path(clip.path)
                if not pl.is_file():
                    self.status.setText(f"Playlist mancante: {clip.path}")
                    return
                try:
                    tracks = parse_playlist(pl)
                    durs = track_durations_ms(tracks)
                except Exception as exc:  # noqa: BLE001
                    self.status.setText(f"Playlist: {exc}")
                    return
                if not tracks:
                    self.status.setText(f"Playlist vuota: {clip.path}")
                    return
                offset = int((now - clip.start_ts).total_seconds() * 1000)
                offset = max(0, offset)
                self._player.play_playlist(
                    [str(t) for t in tracks],
                    durs,
                    clip_id=clip.id,
                    peak_gain=clip.peak_gain,
                    start_offset_ms=offset,
                )
                self.timeline.set_playing_id(clip.id)
                self.status.setText(
                    f"In onda PLAYLIST: {clip.show_title}  "
                    f"[{clip.source_label}]  |  DB: {self.db.path}"
                )
                return
            path = Path(clip.path)
            if not path.is_file():
                self.status.setText(f"File mancante: {clip.path}")
                return
            offset = int((now - clip.start_ts).total_seconds() * 1000)
            offset = max(0, min(offset, max(0, clip.duration_ms - 50)))
            self._player.play_file(
                str(path),
                clip_id=clip.id,
                peak_gain=clip.peak_gain,
                start_offset_ms=offset,
            )
            self.timeline.set_playing_id(clip.id)
            self.status.setText(
                f"In onda: {clip.show_title}  [{clip.source_label}]  "
                f"|  DB: {self.db.path}"
            )
        except Exception as exc:  # noqa: BLE001
            if clip.is_link:
                self._enter_link_failover(clip, str(exc))
            else:
                self.status.setText(f"Errore avvio: {exc}")

    def _scheduler_anti_bianco(
        self, *, link_failover: bool = False, silence_failover: bool = False
    ) -> None:
        """Filler ANTI BIANCO: buco, LINK offline o silenzio sorgente."""
        assert self._player is not None
        path_s = self._anti_bianco_path
        if not path_s:
            if self._player.clip_id is not None:
                self._player.stop()
                self.timeline.set_playing_id(None)
            return
        pl = Path(path_s)
        if not pl.is_file():
            if self._player.clip_id is not None:
                self._player.stop()
                self.timeline.set_playing_id(None)
            self.status.setText(f"ANTI BIANCO: playlist mancante ({path_s})")
            return
        if self._player.clip_id == ANTI_BIANCO_CLIP_ID:
            return
        try:
            tracks = parse_playlist(pl)
            durs = track_durations_ms(tracks)
        except Exception as exc:  # noqa: BLE001
            self.status.setText(f"ANTI BIANCO: {exc}")
            return
        if not tracks:
            self.status.setText(f"ANTI BIANCO: playlist vuota ({pl.name})")
            return
        try:
            self._player.play_playlist(
                [str(t) for t in tracks],
                durs,
                clip_id=ANTI_BIANCO_CLIP_ID,
                peak_gain=1.0,
                start_offset_ms=max(0, int(self._anti_bianco_resume_ms)),
            )
            self.timeline.set_playing_id(None)
            if silence_failover:
                self.status.setText(
                    f"ANTI BIANCO (silenzio): {pl.name}  |  DB: {self.db.path}"
                )
            elif link_failover:
                self.status.setText(
                    f"ANTI BIANCO (LINK offline): {pl.name}  |  DB: {self.db.path}"
                )
            else:
                self.status.setText(
                    f"ANTI BIANCO in onda: {pl.name}  |  DB: {self.db.path}"
                )
        except Exception as exc:  # noqa: BLE001
            self.status.setText(f"ANTI BIANCO errore: {exc}")

    def _refresh_anti_bianco_btn(self) -> None:
        self.btn_anti.setText("ANTI BIANCO")
        if self._anti_bianco_path:
            self.btn_anti.setToolTip(
                f"Playlist filler attiva: {self._anti_bianco_path}\n"
                "In onda nei buchi di palinsesto e se un LINK è offline."
            )
        else:
            self.btn_anti.setToolTip(
                "Scegli una playlist filler per i buchi del palinsesto "
                "e per i LINK offline."
            )

    def _anti_bianco_dialog(self) -> None:
        dlg = AntiBiancoDialog(self._anti_bianco_path, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        new_path = (dlg.path or "").strip()
        if new_path and new_path != self._anti_bianco_path:
            pl = Path(new_path)
            if not pl.is_file():
                QMessageBox.warning(
                    self, "ANTI BIANCO", f"File non trovato:\n{new_path}"
                )
                return
            try:
                tracks = parse_playlist(pl)
            except Exception as exc:  # noqa: BLE001
                QMessageBox.warning(self, "ANTI BIANCO", f"Playlist non valida:\n{exc}")
                return
            if not tracks:
                QMessageBox.warning(
                    self,
                    "ANTI BIANCO",
                    "Playlist vuota o senza file audio locali.",
                )
                return
        prev = self._anti_bianco_path
        self._anti_bianco_path = new_path
        self.db.set_setting(SETTING_ANTI_BIANCO, new_path)
        if new_path != prev:
            # Nuova playlist (o disattiva): ripresa da capo
            self._anti_bianco_resume_ms = 0
        self._refresh_anti_bianco_btn()
        # Se stava suonando il filler e cambia/disattiva → stop; lo scheduler riparte
        if (
            self._running
            and self._player
            and self._player.clip_id == ANTI_BIANCO_CLIP_ID
            and new_path != prev
        ):
            self._player.stop()
            self.timeline.set_playing_id(None)
        if new_path:
            self.status.setText(
                f"ANTI BIANCO impostato: {Path(new_path).name}  |  DB: {self.db.path}"
            )
        else:
            self.status.setText(f"ANTI BIANCO disattivato  |  DB: {self.db.path}")

    def _tick(self) -> None:
        pump_glib_once()
        now = datetime.now()
        self.clock.setText(now.strftime("%a %d/%m/%Y  %H:%M:%S"))
        self.timeline.set_now(now)
        if self._running:
            # Silence failover: poll bus monitor (ripresa affidabile, anche LIVE)
            if (
                self._silence_failover_clip_id is not None
                and self._silence_tap is not None
            ):
                try:
                    polled = self._silence_tap.poll()
                except Exception:
                    polled = None
                if polled is not None:
                    self._silence_monitor_last_db = float(polled)
                    # Non sovrascrivere il VU del filler: mostra il monitor in status
                    self._silence_evaluate_recover(float(polled))
                else:
                    self._silence_evaluate_recover(self._silence_monitor_last_db)
                now_m = time.monotonic()
                if now_m - self._silence_status_at >= 1.0:
                    self._silence_status_at = now_m
                    self.status.setText(
                        f"ANTI BIANCO (silenzio) — monitor "
                        f"{self._silence_monitor_last_db:.1f} dBFS  |  DB: {self.db.path}"
                    )
            if (
                self._player is not None
                and self._player.clip_id == ANTI_BIANCO_CLIP_ID
            ):
                self._anti_bianco_resume_ms = self._player.playlist_position_ms()
            self._scheduler_step()

    def _pick_audio_file(self) -> Path | None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Scegli file audio",
            str(Path.home()),
            "Audio (*.wav *.mp3 *.flac *.ogg *.opus *.m4a *.aac *.wma *.aiff *.aif);;Tutti (*)",
        )
        return Path(path) if path else None

    def _import_at(self, start: datetime) -> None:
        path = self._pick_audio_file()
        if path is None:
            return
        try:
            duration_ms, peak_gain, title, description = probe_audio(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Probe", f"Impossibile analizzare il file:\n{exc}")
            return
        try:
            clip = self.db.add_clip(
                path=path,
                display_name=path.name,
                duration_ms=duration_ms,
                peak_gain=peak_gain,
                start=start,
                title=title,
                description=description,
            )
        except OverlapError as exc:
            QMessageBox.warning(self, "Sovrapposizione", str(exc))
            return
        self._selected_id = clip.id
        # Conferma/modifica titolo e descrizione (tag precompilati)
        self._edit_clip(clip.id, after_add=True)
        self._reload_clips()
        clip = self.db.get_clip(clip.id) or clip
        self.status.setText(
            f"Aggiunto «{clip.show_title}» [{clip.source_label}] "
            f"{clip.start_ts.strftime('%H:%M:%S')}–"
            f"{clip.end_ts.strftime('%H:%M:%S')} "
            f"(gain {peak_gain:.2f})  |  DB: {self.db.path}"
        )

    def _add_on_day(self, day: date) -> None:
        last = self.db.last_clip_ending_on_day(
            datetime.combine(day, datetime.min.time())
        )
        if last is not None:
            initial = last.end_ts
            # Se finisce dopo mezzanotte del giorno, usa fine
            if initial.date() != day:
                initial = datetime.combine(day, datetime.min.time()) + timedelta(seconds=1)
        else:
            now = datetime.now().replace(microsecond=0)
            if now.date() == day:
                initial = now
            else:
                initial = datetime.combine(day, datetime.min.time()) + timedelta(seconds=1)

        dlg = TimePickDialog("Orario inizio", day, initial, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self._import_at(dlg.selected_datetime())

    def _add_dialog_today(self) -> None:
        self._add_on_day(date.today())

    def _add_choice_dialog(self) -> None:
        dlg = AddChoiceDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted or dlg.choice is None:
            return
        if dlg.choice == AddChoiceDialog.CHOICE_FILE:
            self._add_dialog_today()
        elif dlg.choice == AddChoiceDialog.CHOICE_LIVE:
            self._add_live_dialog()
        elif dlg.choice == AddChoiceDialog.CHOICE_LINK:
            self._add_link_dialog()
        elif dlg.choice == AddChoiceDialog.CHOICE_PLAYLIST:
            self._add_playlist_dialog()

    def _default_start_for_day(self, day: date) -> datetime:
        last = self.db.last_clip_ending_on_day(
            datetime.combine(day, datetime.min.time())
        )
        if last is not None:
            initial = last.end_ts
            if initial.date() != day:
                initial = datetime.combine(day, datetime.min.time()) + timedelta(
                    seconds=1
                )
            return initial
        now = datetime.now().replace(microsecond=0)
        if now.date() == day:
            return now
        return datetime.combine(day, datetime.min.time()) + timedelta(seconds=1)

    def _open_mixer(self) -> None:
        MixerDialog(self).exec()

    def _open_settings(self) -> None:
        dlg = SettingsDialog(self.db, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self._reload_silence_settings()
        self._apply_aspect_settings()
        self.status.setText(f"SETTING aggiornati  |  DB: {self.db.path}")

    def _add_live_dialog(self) -> None:
        day = date.today()
        # Se la settimana visualizzata non contiene oggi, usa il lunedì della vista
        monday = self.timeline.week_monday()
        if not (monday <= day <= monday + timedelta(days=6)):
            day = monday
        dlg = LiveSlotDialog(
            day,
            self._default_start_for_day(day),
            self,
            default_device=self._default_live_device,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        start, end, title, description, device, color = dlg.values()
        try:
            clip = self.db.add_live(
                start=start,
                end=end,
                title=title,
                description=description,
                device=device,
                color=color,
            )
        except OverlapError as exc:
            QMessageBox.warning(self, "Sovrapposizione", str(exc))
            return
        except ValueError as exc:
            QMessageBox.warning(self, "LIVE", str(exc))
            return
        self._default_live_device = device
        self.db.set_setting("default_live_device", device)
        self._selected_id = clip.id
        self._reload_clips()
        self.status.setText(
            f"Aggiunto LIVE «{clip.show_title}» "
            f"{clip.start_ts.strftime('%H:%M:%S')}–"
            f"{clip.end_ts.strftime('%H:%M:%S')}  |  DB: {self.db.path}"
        )

    def _add_link_dialog(self) -> None:
        day = date.today()
        monday = self.timeline.week_monday()
        if not (monday <= day <= monday + timedelta(days=6)):
            day = monday
        dlg = LinkSlotDialog(day, self._default_start_for_day(day), self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            start, end, title, description, url, color, peak_gain = dlg.values()
        except ValueError as exc:
            QMessageBox.warning(self, "LINK", str(exc))
            return
        try:
            clip = self.db.add_link(
                start=start,
                end=end,
                url=url,
                title=title,
                description=description,
                color=color,
                peak_gain=peak_gain,
            )
        except OverlapError as exc:
            QMessageBox.warning(self, "Sovrapposizione", str(exc))
            return
        except ValueError as exc:
            QMessageBox.warning(self, "LINK", str(exc))
            return
        self._selected_id = clip.id
        self._reload_clips()
        self.status.setText(
            f"Aggiunto LINK «{clip.show_title}» "
            f"{clip.start_ts.strftime('%H:%M:%S')}–"
            f"{clip.end_ts.strftime('%H:%M:%S')}  |  DB: {self.db.path}"
        )

    def _pick_playlist_file(self) -> Path | None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Scegli playlist",
            str(Path.home()),
            "Playlist (*.m3u *.m3u8 *.pls);;Tutti (*)",
        )
        return Path(path) if path else None

    def _import_playlist_at(self, start: datetime) -> None:
        path = self._pick_playlist_file()
        if path is None:
            return
        try:
            duration_ms, peak_gain, title, description, _tracks, _durs = probe_playlist(
                path
            )
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(
                self, "Playlist", f"Impossibile analizzare la playlist:\n{exc}"
            )
            return
        try:
            clip = self.db.add_playlist(
                path=path,
                display_name=path.name,
                duration_ms=duration_ms,
                peak_gain=peak_gain,
                start=start,
                title=title,
                description=description,
                color=DEFAULT_PLAYLIST_COLOR,
            )
        except OverlapError as exc:
            QMessageBox.warning(self, "Sovrapposizione", str(exc))
            return
        self._selected_id = clip.id
        self._edit_clip(clip.id, after_add=True)
        self._reload_clips()
        clip = self.db.get_clip(clip.id) or clip
        self.status.setText(
            f"Aggiunta PLAYLIST «{clip.show_title}» [{clip.source_label}] "
            f"{clip.start_ts.strftime('%H:%M:%S')}–"
            f"{clip.end_ts.strftime('%H:%M:%S')} "
            f"(gain {peak_gain:.2f})  |  DB: {self.db.path}"
        )

    def _add_playlist_dialog(self) -> None:
        day = date.today()
        monday = self.timeline.week_monday()
        if not (monday <= day <= monday + timedelta(days=6)):
            day = monday
        dlg = TimePickDialog("Orario inizio", day, self._default_start_for_day(day), self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self._import_playlist_at(dlg.selected_datetime())

    def _add_after_clip(self, clip_id: int) -> None:
        clip = self.db.get_clip(clip_id)
        if clip is None:
            return
        self._import_at(clip.end_ts)

    def _edit_clip(self, clip_id: int, *, after_add: bool = False) -> None:
        clip = self.db.get_clip(clip_id)
        if clip is None:
            return
        dlg = EditClipDialog(clip, self)
        if after_add:
            dlg.setWindowTitle("Nuova trasmissione — conferma dettagli")
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        vals = dlg.values()
        try:
            self.db.update_clip(clip_id, **vals)
        except OverlapError as exc:
            QMessageBox.warning(self, "Sovrapposizione", str(exc))
            return
        except ValueError as exc:
            QMessageBox.warning(self, "Modifica", str(exc))
            return
        device = vals.get("device")
        if device is not None:
            self._default_live_device = (
                device if str(device).startswith("pulse:") else f"pulse:{device}"
            )
            self.db.set_setting("default_live_device", self._default_live_device)
        self._reload_clips()
        self._select_clip(clip_id)

    def _delete_selected(self) -> None:
        """Elimina il clip selezionato (usato dal popup dettaglio)."""
        if self._selected_id is None:
            return
        clip = self.db.get_clip(self._selected_id)
        if clip is None:
            return
        if (
            QMessageBox.question(
                self,
                "Elimina",
                f"Eliminare «{clip.show_title}»?",
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        if self._player and self._player.clip_id == clip.id:
            self._player.stop()
        self.db.delete_clip(clip.id)
        self._selected_id = None
        self._reload_clips()

    def closeEvent(self, event) -> None:  # noqa: N802
        self._running = False
        self._clear_silence_failover()
        self._clear_link_failover()
        if self._player:
            self._player.stop()
        try:
            self._persist_settings()
        except Exception:
            pass
        self.db.close()
        super().closeEvent(event)


def run_app() -> int:
    app = QApplication([])
    app.setApplicationName("Quelo-palinsesto-radio")
    win = MainWindow()
    if getattr(win, "_want_maximized", True):
        win.showMaximized()
    else:
        win.show()
    # Autostart interno: palinsesto in onda appena parte l'app
    QTimer.singleShot(0, win._start)
    return app.exec()
