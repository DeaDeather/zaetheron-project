"""
dynamic_island.py — «Dynamic Island» для Zaetheron.

Плавающий значок в правом верхнем углу экрана:
  • свёрнут — маленькая серая полупрозрачная «таблетка» с логотипом,
    мерцающими звёздами и голубой точкой-индикатором внутри;
  • по клику плавно (не сильно) раскрывается на 5 секунд и показывает,
    что сейчас играет — Spotify / Яндекс.Музыка / VK Музыка / браузер —
    и громкость, регулируемую как на iPhone;
  • перетаскивается мышью (зажать и потянуть).

Определение трека берётся из системного Windows SMTC
(GlobalSystemMediaTransportControlsSessionManager) — это единый API,
которым пользуется сам Windows для показа плашки "сейчас играет" на
клавиатурных медиа-кнопках и в Центре уведомлений. Он автоматически
работает со Spotify (десктоп), Яндекс.Музыкой и VK Музыкой (десктоп-
приложения и веб-плееры в Chrome/Edge/Firefox) — ничего дополнительно
подключать не нужно. Если ни один плеер не публикует эти данные —
остров просто показывает "Ничего не играет" и не мешает работе.

Требует (только Windows, при отсутствии — модуль тихо отключает
соответствующую возможность и ничего не ломает):
    pip install winsdk pycaw comtypes
"""

import os
import sys
import math
import random
import threading
import time

from PyQt6.QtWidgets import QWidget, QSlider, QApplication
from PyQt6.QtCore import (
    Qt, QTimer, QPropertyAnimation, QEasingCurve, pyqtProperty,
    pyqtSignal, QObject, QRectF, QPointF,
)
from PyQt6.QtGui import (
    QPainter, QColor, QBrush, QPen, QPainterPath, QLinearGradient,
    QRadialGradient, QPixmap, QFont, QFontMetrics,
)

# ------------------------------------------------------------------ #
#  Необязательные платформенные интеграции (только Windows)          #
# ------------------------------------------------------------------ #
try:
    import asyncio
    from winsdk.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as _MediaManager,
    )
    _HAS_WINSDK = True
except Exception:
    _HAS_WINSDK = False

try:
    from ctypes import cast, POINTER
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    _HAS_PYCAW = True
except Exception:
    _HAS_PYCAW = False


_APP_NAME_MAP = {
    "spotify": "Spotify",
    "yandexmusic": "Яндекс Музыка",
    "yandex": "Яндекс Музыка",
    "vkmusic": "VK Музыка",
    "vk.": "VK Музыка",
    "chrome": "Браузер",
    "msedge": "Браузер",
    "firefox": "Браузер",
}


def _pretty_app(aumid: str) -> str:
    low = (aumid or "").lower()
    for key, val in _APP_NAME_MAP.items():
        if key in low:
            return val
    return "Проигрыватель"


class _MediaBridge(QObject):
    """Сигналы приходят из фонового потока в поток интерфейса (Qt делает это безопасно сам)."""
    track_changed = pyqtSignal(str, str, str, bool)   # title, artist, app, is_playing
    track_cleared = pyqtSignal()


class _MediaWatcher(threading.Thread):
    """Опрашивает Windows SMTC в фоне, не блокируя интерфейс."""

    def __init__(self, bridge: _MediaBridge, interval: float = 1.2):
        super().__init__(daemon=True)
        self.bridge = bridge
        self.interval = interval
        self._stop = False
        self._last = None
        self._mgr = None

    def stop(self):
        self._stop = True

    def run(self):
        if not _HAS_WINSDK:
            return
        # WinRT (winsdk) требует явной инициализации COM/WinRT-апартамента
        # на КАЖДОМ потоке, который его использует — это происходит
        # автоматически только для того потока, что первым импортировал
        # модуль (обычно главный поток приложения). Этот поток — отдельный
        # threading.Thread, апартамент здесь никогда не инициализирован,
        # поэтому без явного вызова request_async() тихо падал на каждой
        # попытке (см. комментарий про except Exception ниже) — из-за
        # этого остров никогда не видел, что сейчас играет.
        try:
            from winsdk._winrt import init_apartment, MTA
            init_apartment(MTA)
        except Exception:
            pass
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        while not self._stop:
            try:
                info = loop.run_until_complete(self._read_once())
                if info:
                    key = info
                    if key != self._last:
                        self._last = key
                        title, artist, app, playing = info
                        self.bridge.track_changed.emit(title, artist, app, playing)
                else:
                    if self._last is not None:
                        self._last = None
                        self.bridge.track_cleared.emit()
            except Exception:
                # что-то пошло не так (например, менеджер сессий устарел
                # после сна/пробуждения ПК) — забываем кэш и переподключимся
                # на следующей итерации
                self._mgr = None
            time.sleep(self.interval)

    async def _get_manager(self):
        if self._mgr is None:
            self._mgr = await _MediaManager.request_async()
        return self._mgr

    async def _read_once(self):
        mgr = await self._get_manager()

        try:
            sessions = list(mgr.get_sessions())
        except Exception:
            sessions = []

        # Windows считает "текущей" ту сессию, которая последней получила
        # фокус медиа-клавиш — это НЕ обязательно та, что реально играет
        # прямо сейчас. Поэтому сперва ищем среди всех сессий ту, что
        # реально в статусе Playing, и только если такой нет — берём
        # "текущую" как раньше.
        session = None
        for s in sessions:
            try:
                info = s.get_playback_info()
                if int(info.playback_status) == 4:  # Playing
                    session = s
                    break
            except Exception:
                continue

        if session is None:
            try:
                session = mgr.get_current_session()
            except Exception:
                session = None

        if session is None:
            return None

        props = await session.try_get_media_properties_async()
        title = (props.title or "").strip()
        artist = (props.artist or "").strip()
        if not title:
            return None
        aumid = ""
        try:
            aumid = session.source_app_user_model_id or ""
        except Exception:
            pass
        playing = False
        try:
            info = session.get_playback_info()
            playing = int(info.playback_status) == 4  # Playing
        except Exception:
            pass
        return title, artist, _pretty_app(aumid), playing


class _VolumeCtl:
    """
    Обёртка над системной громкостью Windows (через pycaw).
    Инициализация COM-эндпоинта иногда не успевает подняться на самом
    старте приложения (звуковое устройство ещё не готово / смена
    устройства по умолчанию) — раньше в этом случае _vol навсегда
    оставался None и ползунок ничего не делал. Теперь при любой неудаче
    просто сбрасываем указатель и пробуем переподключиться при следующем
    обращении (get/set), а не один раз при создании объекта.
    """

    def __init__(self):
        self._vol = None
        self._try_init()

    def _try_init(self):
        if not _HAS_PYCAW:
            return
        # comtypes сам инициализирует COM автоматически, но только для
        # ТОГО потока, который первым импортировал модуль comtypes — как
        # правило это главный поток. Если _VolumeCtl создаётся не на нём
        # (или в собранном Nuitka .exe порядок/поток импорта отличается
        # от обычного запуска через "py zaetheron.py"), Activate() падает
        # с "CoInitialize has not been called" — тихо, из-за try/except
        # ниже, и громкость навсегда остаётся заглушкой в 50%. Поэтому
        # явно вызываем CoInitialize здесь же, перед первым использованием.
        try:
            import comtypes
            comtypes.CoInitialize()
        except Exception:
            pass  # уже инициализирован на этом потоке — это нормально
        try:
            speakers = AudioUtilities.GetSpeakers()
            interface = speakers.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            self._vol = cast(interface, POINTER(IAudioEndpointVolume))
        except Exception:
            self._vol = None

    @property
    def available(self) -> bool:
        return self._vol is not None

    def get(self) -> int:
        if not self._vol:
            self._try_init()
        if not self._vol:
            return 50
        try:
            return int(round(self._vol.GetMasterVolumeLevelScalar() * 100))
        except Exception:
            self._vol = None
            return 50

    def set(self, pct: int):
        if not self._vol:
            self._try_init()
        if not self._vol:
            return
        try:
            self._vol.SetMasterVolumeLevelScalar(max(0, min(100, pct)) / 100.0, None)
        except Exception:
            self._vol = None

    def get_mute(self) -> bool:
        if not self._vol:
            self._try_init()
        if not self._vol:
            return False
        try:
            return bool(self._vol.GetMute())
        except Exception:
            self._vol = None
            return False

    def set_mute(self, muted: bool):
        if not self._vol:
            self._try_init()
        if not self._vol:
            return
        try:
            self._vol.SetMute(1 if muted else 0, None)
        except Exception:
            self._vol = None


class DynamicIslandWidget(QWidget):
    """
    «Dynamic Island» Zaetheron — floating виджет поверх всех окон.
    Полностью совместим по конструктору (parent=None) и поведению
    перетаскивания с прежним значком-логотипом.
    """

    COLLAPSED_W, COLLAPSED_H = 150, 40
    EXPANDED_W, EXPANDED_H = 340, 96
    LOGO_SIZE = 30
    EXPAND_MS = 5000  # держим раскрытым 5 секунд без взаимодействия

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMouseTracking(True)

        self._logo_px = self._load_logo()

        # холст фиксирован под максимальный (раскрытый) размер,
        # реальные видимые размеры пилюли — self._w / self._h
        self.setFixedSize(self.EXPANDED_W + 4, self.EXPANDED_H + 4)

        screen = QApplication.primaryScreen().geometry()
        self._anchor_x = screen.width() - self.EXPANDED_W - 20
        self._anchor_y = 8

        self._progress = 0.0
        self._w = float(self.COLLAPSED_W)
        self._h = float(self.COLLAPSED_H)
        self.move(int(self._anchor_x + (self.EXPANDED_W - self._w)), self._anchor_y)

        self._expanded = False
        self._drag_pos = None
        self._dragged = False

        # звёзды и голубые частицы, летающие внутри острова
        self._stars = [self._new_star() for _ in range(16)]
        self._particles = [self._new_particle() for _ in range(10)]

        # редкие «кометы», пролетающие через остров по диагонали
        self._comets = []
        self._next_comet_at = time.time() + random.uniform(5.0, 11.0)

        # редко мигающая голубая точка-индикатор
        self._dot_blink = 0.0
        self._blink_start = None
        self._next_blink_at = time.time() + random.uniform(2.0, 4.5)

        # анимация раскрытия/сворачивания
        self._anim = QPropertyAnimation(self, b"islandProgress")
        self._anim.setDuration(260)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._collapse_timer = QTimer(self)
        self._collapse_timer.setSingleShot(True)
        self._collapse_timer.timeout.connect(self.collapse)

        # ---- медиа (Spotify / Яндекс.Музыка / VK Музыка / браузер) ----
        self._track_title = ""
        self._track_artist = ""
        self._track_app = ""
        self._track_playing = False
        self._bridge = _MediaBridge()
        self._bridge.track_changed.connect(self._on_track_changed)
        self._bridge.track_cleared.connect(self._on_track_cleared)
        self._watcher = _MediaWatcher(self._bridge)
        if _HAS_WINSDK:
            self._watcher.start()

        # ---- громкость, как на iPhone ----
        self._volume_ctl = _VolumeCtl()
        self._volume_val = self._volume_ctl.get()
        self._volume_muted = self._volume_ctl.get_mute()
        self._pre_mute_val = self._volume_val
        self._volume_slider = self._build_volume_slider()

        # ---- отрисовка частиц/звёзд/мигания ----
        self._tick_timer = QTimer(self)
        self._tick_timer.timeout.connect(self._tick)
        self._tick_timer.start(33)  # ~30 fps — достаточно плавно и легко для CPU

    # ------------------------------------------------------------------ загрузка логотипа
    def _load_logo(self):
        base = os.path.dirname(os.path.abspath(sys.argv[0]))
        for name in ("icon.png", "logo.png", "icon.ico"):
            path = os.path.join(base, name)
            if os.path.exists(path):
                px = QPixmap(path)
                if not px.isNull():
                    return px.scaled(
                        self.LOGO_SIZE, self.LOGO_SIZE,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
        return None

    # ------------------------------------------------------------------ анимируемое свойство раскрытия
    def getIslandProgress(self):
        return self._progress

    def setIslandProgress(self, v):
        self._progress = float(v)
        self._w = self.COLLAPSED_W + (self.EXPANDED_W - self.COLLAPSED_W) * self._progress
        self._h = self.COLLAPSED_H + (self.EXPANDED_H - self.COLLAPSED_H) * self._progress
        x = self._anchor_x + (self.EXPANDED_W - self._w)
        self.move(int(x), int(self._anchor_y))
        if self._progress > 0.5:
            self._position_volume_slider()
            self._volume_slider.show()
        else:
            self._volume_slider.hide()
        self.update()

    islandProgress = pyqtProperty(float, getIslandProgress, setIslandProgress)

    # ------------------------------------------------------------------ раскрытие / сворачивание
    def expand(self):
        self._expanded = True
        # синхронизируемся с реальной системной громкостью — она могла
        # измениться клавишами клавиатуры/другим приложением, пока остров
        # был свёрнут
        try:
            sys_vol = self._volume_ctl.get()
            sys_muted = self._volume_ctl.get_mute()
            self._volume_muted = sys_muted
            if not sys_muted:
                self._volume_val = sys_vol
                self._volume_slider.blockSignals(True)
                self._volume_slider.setValue(sys_vol)
                self._volume_slider.blockSignals(False)
        except Exception:
            pass
        self._anim.stop()
        self._anim.setStartValue(self._progress)
        self._anim.setEndValue(1.0)
        self._anim.start()
        self._collapse_timer.start(self.EXPAND_MS)

    def collapse(self):
        if not self._expanded:
            return
        self._expanded = False
        self._anim.stop()
        self._anim.setStartValue(self._progress)
        self._anim.setEndValue(0.0)
        self._anim.start()

    def _speaker_rect(self):
        return QRectF(14, self._h - 30, 20, 18)

    def _draw_speaker_icon(self, p: QPainter, rect: QRectF, muted: bool):
        """
        Компактная векторная иконка динамика — рисуется самим PyQt, а не
        системным emoji-шрифтом телефона: корпус-«мегафон» + дуги громкости
        (или тонкая диагональная черта при mute), с лёгким свечением в тон
        голубой точке-индикатору острова.
        """
        cx = rect.left() + 1.5
        cy = rect.center().y()

        color = QColor(230, 210, 255, 255) if muted else QColor(215, 222, 235, 255)
        glow_color = QColor(155, 108, 255, 55) if muted else QColor(120, 175, 255, 50)

        p.setPen(Qt.PenStyle.NoPen)
        glow = QRadialGradient(QPointF(cx + 6, cy), 11)
        glow.setColorAt(0.0, glow_color)
        glow.setColorAt(1.0, QColor(glow_color.red(), glow_color.green(), glow_color.blue(), 0))
        p.setBrush(glow)
        p.drawEllipse(QPointF(cx + 6, cy), 11, 11)

        # корпус динамика: маленький прямоугольник-«ножка» + раструб
        body = QPainterPath()
        body.moveTo(cx, cy - 3.0)
        body.lineTo(cx + 3.0, cy - 3.0)
        body.lineTo(cx + 7.2, cy - 6.6)
        body.lineTo(cx + 7.2, cy + 6.6)
        body.lineTo(cx + 3.0, cy + 3.0)
        body.lineTo(cx, cy + 3.0)
        body.closeSubpath()
        p.setBrush(color)
        p.drawPath(body)

        pen = QPen(color, 1.4)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)

        if muted:
            # диагональная черта вместо перечёркнутого emoji-крестика
            p.drawLine(QPointF(cx + 9.0, cy - 6.2), QPointF(cx + 15.0, cy + 6.2))
        else:
            # 1 или 2 дуги громкости — в зависимости от текущего уровня
            arcs = 1 if self._volume_val < 50 else 2
            for i in range(arcs):
                r = 4.2 + i * 4.0
                arc_rect = QRectF(cx + 7.2 - r, cy - r, r * 2, r * 2)
                p.drawArc(arc_rect, -55 * 16, 110 * 16)
        p.setPen(Qt.PenStyle.NoPen)

    # ------------------------------------------------------------------ перетаскивание / клик
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            if self._expanded and self._speaker_rect().contains(e.position()):
                self._drag_pos = None
                self._dragged = False
                return
            self._drag_pos = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self._dragged = False

    def mouseMoveEvent(self, e):
        if self._drag_pos and e.buttons() == Qt.MouseButton.LeftButton:
            new_pos = e.globalPosition().toPoint() - self._drag_pos
            if (new_pos - self.pos()).manhattanLength() > 3:
                self._dragged = True
            self.move(new_pos)
            # якорь двигается вместе с окном, чтобы раскрытие продолжало расти корректно
            self._anchor_x = new_pos.x() - (self.EXPANDED_W - self._w)
            self._anchor_y = new_pos.y()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and not self._dragged:
            if self._expanded and self._speaker_rect().contains(e.position()):
                self._toggle_mute()
            elif self._expanded:
                self.collapse()
            else:
                self.expand()
        self._drag_pos = None
        self._dragged = False

    def _toggle_mute(self):
        if not self._volume_muted:
            self._pre_mute_val = self._volume_val
            self._volume_ctl.set_mute(True)
            self._volume_muted = True
        else:
            self._volume_ctl.set_mute(False)
            restore = self._pre_mute_val if self._pre_mute_val > 0 else 50
            self._volume_ctl.set(restore)
            self._volume_val = restore
            self._volume_muted = False
        self._volume_slider.blockSignals(True)
        self._volume_slider.setValue(0 if self._volume_muted else self._volume_val)
        self._volume_slider.blockSignals(False)
        self._collapse_timer.start(self.EXPAND_MS)
        self.update()

    # ------------------------------------------------------------------ громкость
    def _build_volume_slider(self):
        s = QSlider(Qt.Orientation.Horizontal, self)
        s.setMinimum(0)
        s.setMaximum(100)
        s.setValue(self._volume_val)
        s.setFixedHeight(18)
        s.setStyleSheet("""
            QSlider::groove:horizontal {
                height: 4px; background: rgba(255,255,255,60); border-radius: 2px;
            }
            QSlider::sub-page:horizontal {
                background: rgba(155,108,255,230); border-radius: 2px;
            }
            QSlider::add-page:horizontal {
                background: rgba(255,255,255,40); border-radius: 2px;
            }
            QSlider::handle:horizontal {
                background: white; width: 14px; height: 14px;
                margin: -6px 0; border-radius: 7px;
            }
        """)
        s.hide()
        s.valueChanged.connect(self._on_volume_changed)
        s.sliderPressed.connect(lambda: self._collapse_timer.stop())
        s.sliderReleased.connect(lambda: self._collapse_timer.start(self.EXPAND_MS))
        return s

    def _position_volume_slider(self):
        pad = 14
        y = int(self._h - 28)
        w = int(self._w - pad * 2 - 40)
        if w < 10:
            w = 10
        self._volume_slider.setGeometry(pad + 20, y, w, 18)

    def _on_volume_changed(self, val):
        self._volume_val = val
        self._volume_muted = False
        self._volume_ctl.set(val)
        self._volume_ctl.set_mute(False)
        self.update()

    # ------------------------------------------------------------------ медиа-колбэки
    def _on_track_changed(self, title, artist, app, playing):
        self._track_title = title
        self._track_artist = artist
        self._track_app = app
        self._track_playing = playing
        self.update()

    def _on_track_cleared(self):
        self._track_title = ""
        self._track_artist = ""
        self._track_app = ""
        self._track_playing = False
        self.update()

    # ------------------------------------------------------------------ частицы / звёзды / мигание
    def _new_star(self):
        # лёгкий разброс оттенков — не только белые «пиксели», а слабая
        # смесь голубых, фиолетовых и тёплых звёзд (эффект туманности)
        tint = random.choice([
            (220, 235, 255),   # холодно-белый
            (196, 178, 255),   # лиловый
            (255, 231, 205),   # тёплый жёлто-белый
        ])
        return {
            "x": random.uniform(4, self.EXPANDED_W - 4),
            "y": random.uniform(4, self.EXPANDED_H - 4),
            "r": random.uniform(0.6, 1.6),
            "phase": random.uniform(0, 6.28),
            "speed": random.uniform(0.02, 0.06),
            "tint": tint,
        }

    def _new_comet(self):
        # комета влетает слева направо по пологой диагонали и исчезает
        # за правым краем острова
        return {
            "x": -12.0,
            "y": random.uniform(6, self.EXPANDED_H * 0.55),
            "vx": random.uniform(2.4, 3.4),
            "vy": random.uniform(0.25, 0.85),
        }

    def _new_particle(self, from_bottom=False):
        return {
            "x": random.uniform(10, self.EXPANDED_W - 10),
            "y": self.EXPANDED_H + random.uniform(0, 20) if from_bottom else random.uniform(10, self.EXPANDED_H - 10),
            "speed": random.uniform(0.15, 0.4),
            "r": random.uniform(1.4, 3.0),
            "alpha": random.uniform(90, 180),
        }

    def _tick(self):
        now = time.time()

        for s in self._stars:
            s["phase"] += s["speed"]

        for pt in self._particles:
            pt["y"] -= pt["speed"]
            if pt["y"] < -6:
                pt.update(self._new_particle(from_bottom=True))

        if now >= self._next_comet_at:
            self._comets.append(self._new_comet())
            self._next_comet_at = now + random.uniform(8.0, 18.0)
        for c in self._comets[:]:
            c["x"] += c["vx"]
            c["y"] += c["vy"]
            if c["x"] > self.EXPANDED_W + 20:
                self._comets.remove(c)

        if self._blink_start is None and now >= self._next_blink_at:
            self._blink_start = now
        if self._blink_start is not None:
            dt = now - self._blink_start
            blink_dur = 0.6
            if dt < blink_dur:
                self._dot_blink = math.sin((dt / blink_dur) * math.pi)
            else:
                self._dot_blink = 0.0
                self._blink_start = None
                self._next_blink_at = now + random.uniform(2.5, 5.0)

        self.update()

    # ------------------------------------------------------------------ отрисовка
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        w, h = self._w, self._h
        radius = h / 2.0
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, w, h), radius, radius)
        p.setClipPath(path)

        # фон — тёмно-синее/фиолетовое «стекло» (Apple-style frosted glass)
        bg = QLinearGradient(0, 0, 0, h)
        bg.setColorAt(0.0, QColor(46, 30, 76, 215))
        bg.setColorAt(0.55, QColor(28, 20, 52, 220))
        bg.setColorAt(1.0, QColor(18, 13, 36, 225))
        p.fillPath(path, QBrush(bg))

        # тонкий диагональный фиолетово-синий подсвет (имитация бликующего стекла)
        sheen = QLinearGradient(0, 0, w, h)
        sheen.setColorAt(0.0, QColor(155, 108, 255, 26))
        sheen.setColorAt(0.5, QColor(91, 127, 255, 10))
        sheen.setColorAt(1.0, QColor(155, 108, 255, 0))
        p.fillPath(path, QBrush(sheen))

        hl = QLinearGradient(0, 0, 0, h * 0.55)
        hl.setColorAt(0.0, QColor(255, 255, 255, 32))
        hl.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.fillRect(QRectF(0, 0, w, h * 0.55), QBrush(hl))

        # тонкая обводка стекла — едва заметный фиолетовый контур
        p.setPen(QPen(QColor(155, 108, 255, 40), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), radius, radius)

        p.setPen(Qt.PenStyle.NoPen)

        # звёзды (лёгкая смесь оттенков — эффект туманности)
        for s in self._stars:
            if s["x"] > w - 4:
                continue
            tw = 0.55 + 0.45 * math.sin(s["phase"])
            tr, tg, tb = s.get("tint", (220, 235, 255))
            p.setBrush(QColor(tr, tg, tb, int(60 * tw + 30)))
            p.drawEllipse(QPointF(s["x"], s["y"]), s["r"], s["r"])

        # редкая комета — короткий светящийся хвост + яркая головка
        for c in self._comets:
            if c["x"] < -12 or c["x"] > w + 4:
                continue
            tail = 20.0
            ang = math.atan2(c["vy"], c["vx"])
            tx = c["x"] - tail * math.cos(ang)
            ty = c["y"] - tail * math.sin(ang)
            trail = QLinearGradient(QPointF(tx, ty), QPointF(c["x"], c["y"]))
            trail.setColorAt(0.0, QColor(190, 210, 255, 0))
            trail.setColorAt(1.0, QColor(215, 225, 255, 200))
            pen = QPen(QBrush(trail), 1.5)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.drawLine(QPointF(tx, ty), QPointF(c["x"], c["y"]))
            p.setPen(Qt.PenStyle.NoPen)
            head_glow = QRadialGradient(QPointF(c["x"], c["y"]), 4.0)
            head_glow.setColorAt(0.0, QColor(230, 240, 255, 200))
            head_glow.setColorAt(1.0, QColor(230, 240, 255, 0))
            p.setBrush(head_glow)
            p.drawEllipse(QPointF(c["x"], c["y"]), 4.0, 4.0)
            p.setBrush(QColor(255, 255, 255, 235))
            p.drawEllipse(QPointF(c["x"], c["y"]), 1.2, 1.2)

        # голубые частицы
        for pt in self._particles:
            if pt["x"] > w - 4:
                continue
            glow = QRadialGradient(QPointF(pt["x"], pt["y"]), pt["r"] * 4)
            glow.setColorAt(0.0, QColor(110, 180, 255, int(pt["alpha"] * 0.55)))
            glow.setColorAt(1.0, QColor(110, 180, 255, 0))
            p.setBrush(glow)
            p.drawEllipse(QPointF(pt["x"], pt["y"]), pt["r"] * 4, pt["r"] * 4)
            p.setBrush(QColor(160, 210, 255, int(pt["alpha"])))
            p.drawEllipse(QPointF(pt["x"], pt["y"]), pt["r"], pt["r"])

        # мигающая белая точка-индикатор (чуть правее самого левого края)
        dot_x, dot_y = 12.0, h / 2.0
        rr = 10 + 14 * self._dot_blink
        glow = QRadialGradient(QPointF(dot_x, dot_y), rr)
        glow.setColorAt(0.0, QColor(255, 255, 255, int(60 + 150 * self._dot_blink)))
        glow.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.setBrush(glow)
        p.drawEllipse(QPointF(dot_x, dot_y), rr, rr)
        p.setBrush(QColor(255, 255, 255, 235))
        p.drawEllipse(QPointF(dot_x, dot_y), 3.0, 3.0)

        # логотип
        logo_x = 34.0
        if self._logo_px:
            logo_y = (h - self._logo_px.height()) / 2.0
            p.drawPixmap(int(logo_x), int(logo_y), self._logo_px)
            text_x = logo_x + self._logo_px.width() + 10
        else:
            text_x = logo_x + self.LOGO_SIZE + 10

        # текст трека + громкость — проявляются по мере раскрытия
        if self._progress > 0.25:
            alpha = min(1.0, (self._progress - 0.25) / 0.5)
            p.setOpacity(alpha)

            title = self._track_title or "Ничего не играет"
            if self._track_title:
                subtitle = self._track_artist + ("  ·  " + self._track_app if self._track_app else "")
            else:
                subtitle = "Spotify / Яндекс.Музыка / VK Музыка"

            title_font = QFont(self.font().family(), 10, QFont.Weight.DemiBold)
            p.setFont(title_font)
            p.setPen(QColor(235, 240, 245, 255))
            title_w = max(10, w - text_x - 14)
            elided = QFontMetrics(title_font).elidedText(title, Qt.TextElideMode.ElideRight, int(title_w))
            p.drawText(QRectF(text_x, 8, title_w, 16),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, elided)

            sub_font = QFont(self.font().family(), 8)
            p.setFont(sub_font)
            p.setPen(QColor(170, 180, 195, 255))
            elided_sub = QFontMetrics(sub_font).elidedText(subtitle, Qt.TextElideMode.ElideRight, int(title_w))
            p.drawText(QRectF(text_x, 24, title_w, 14),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, elided_sub)

            # громкость: векторная иконка-«динамик», нарисованная самим
            # виджетом (без системных emoji-шрифтов), кликабельна — тап
            # мьютит/размьючивает, как в Пункте управления iPhone
            self._draw_speaker_icon(p, self._speaker_rect(), self._volume_muted)

            vol_font = QFont(self.font().family(), 9)
            p.setFont(vol_font)
            p.setPen(QColor(160, 150, 190, 220) if self._volume_muted else QColor(200, 210, 225, 255))
            vol_text = "Mute" if self._volume_muted else f"{self._volume_val}%"
            p.drawText(QRectF(w - 46, h - 30, 32, 18),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, vol_text)

            p.setOpacity(1.0)

    # ------------------------------------------------------------------ завершение работы
    def closeEvent(self, e):
        try:
            self._watcher.stop()
        except Exception:
            pass
        try:
            self._tick_timer.stop()
        except Exception:
            pass
        super().closeEvent(e)
