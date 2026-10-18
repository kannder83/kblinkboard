"""PySide6 window and menu bar icon for the TH80 lights."""

import colorsys
import math
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QMainWindow,
    QMenu,
    QPushButton,
    QSlider,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from kblinkboard.device import MODE_OPTIONS, MODES, PRESET_COLORS, Keyboard, KeyboardError, LedParam

MODE_LABELS = {
    "estatico": "Estático",
    "respiracion": "Respiración",
    "espectro": "Espectro",
    "onda": "Onda",
    "ondulacion": "Ondulación (al pulsar)",
    "estrellas": "Estrellas",
    "flujo": "Flujo",
    "sombra": "Sombra de tecla (al pulsar)",
    "capas": "Capas",
    "seno": "Seno",
    "resorte": "Resorte",
    "neon": "Neón",
    "radiante": "Radiante",
    "bucle": "Bucle",
    "cuadricula": "Cuadrícula",
    "nevada": "Nevada",
    "meteoro": "Meteoro",
    "nieve": "Nieve silenciosa",
}

# Wait this long after the last control change before writing, so dragging a
# slider sends one report instead of dozens (fast writes stall the board).
APPLY_DELAY_MS = 350


@dataclass(frozen=True)
class Preset:
    name: str
    mode: str
    rainbow: bool = False
    color: tuple[int, int, int] = (255, 0, 0)
    speed: int = 3


PRESETS = [
    Preset("Ondulación arcoíris", "ondulacion", rainbow=True),
    Preset("Ondulación roja", "ondulacion", color=(255, 0, 0)),
    Preset("Onda arcoíris", "onda", rainbow=True),
    Preset("Respiración azul", "respiracion", color=(0, 128, 255), speed=2),
    Preset("Azul fijo", "estatico", color=(0, 128, 255)),
    Preset("Blanco fijo", "estatico", color=(255, 255, 255)),
]


# ---------------------------------------------------------------- layout

def _row(y: float, keys: list[tuple[str | None, float]]) -> list[tuple[str, float, float, float]]:
    out, x = [], 0.0
    for label, w in keys:
        if label is not None:
            out.append((label, x, y, w))
        x += w
    return out


_FN = [(f"F{i}", 1) for i in range(1, 13)]
TH80_KEYS = (
    _row(0, [("Esc", 1), (None, 0.25), *_FN[0:4], (None, 0.25), *_FN[4:8], (None, 0.25), *_FN[8:12]])
    + _row(1.25, [(k, 1) for k in "`1234567890-="] + [("⌫", 2), ("Del", 1)])
    + _row(2.25, [("Tab", 1.5)] + [(k, 1) for k in "QWERTYUIOP[]"] + [("\\", 1.5), ("PgUp", 1)])
    + _row(3.25, [("Caps", 1.75)] + [(k, 1) for k in "ASDFGHJKL;'"] + [("Enter", 2.25), ("PgDn", 1)])
    + _row(4.25, [("Shift", 2.25)] + [(k, 1) for k in "ZXCVBNM,./"] + [("Shift", 1.75), ("↑", 1)])
    + _row(5.25, [("Ctrl", 1.25), ("⌥", 1.25), ("⌘", 1.25), ("", 6.25), ("⌥", 1), ("Fn", 1),
                  ("Ctrl", 1), ("←", 1), ("↓", 1), ("→", 1)])
)
LAYOUT_W, LAYOUT_H = 16.0, 6.25
KNOB = (15.4, 0.5)

_KEY_INDEX: dict[str, int] = {}
for _i, (_label, *_rest) in enumerate(TH80_KEYS):
    _KEY_INDEX.setdefault(_label, _i)

_SHIFTED = dict(zip('~!@#$%^&*()_+{}|:"<>?', "`1234567890-=[]\\;',./"))

# On macOS Qt reports ⌘ as Key_Control and Ctrl as Key_Meta.
_QT_KEYS = {
    Qt.Key_Escape: "Esc",
    Qt.Key_Backspace: "⌫",
    Qt.Key_Tab: "Tab",
    Qt.Key_Return: "Enter",
    Qt.Key_Enter: "Enter",
    Qt.Key_Space: "",
    Qt.Key_Delete: "Del",
    Qt.Key_PageUp: "PgUp",
    Qt.Key_PageDown: "PgDn",
    Qt.Key_Up: "↑",
    Qt.Key_Down: "↓",
    Qt.Key_Left: "←",
    Qt.Key_Right: "→",
    Qt.Key_Shift: "Shift",
    Qt.Key_Control: "⌘",
    Qt.Key_Meta: "Ctrl",
    Qt.Key_Alt: "⌥",
    Qt.Key_CapsLock: "Caps",
    **{getattr(Qt, f"Key_F{i}"): f"F{i}" for i in range(1, 13)},
}


def key_for_event(event) -> int | None:
    label = _QT_KEYS.get(event.key())
    if label is None:
        text = event.text().upper()
        label = _SHIFTED.get(text, text)
    return _KEY_INDEX.get(label)


def hue(h: float) -> QColor:
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, 1.0, 1.0)
    return QColor(int(r * 255), int(g * 255), int(b * 255))


def mix(a: QColor, b: QColor, t: float) -> QColor:
    return QColor(*(int(x + (y - x) * t) for x, y in zip(a.getRgb()[:3], b.getRgb()[:3])))


# ---------------------------------------------------------------- preview

class KeyboardPreview(QWidget):
    """A drawn TH80 that approximates what the board's firmware will show."""

    CAP = QColor("#26262a")
    BODY = QColor("#141416")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.led = LedParam(1, 2, 3, 0, False, 0, 128, 255)
        self.backlight = True
        self.ripples: list[tuple[float, float, float]] = []  # x, y, start
        self.pressed: dict[int, float] = {}  # key index -> press time
        self.setMinimumSize(620, 250)
        self.setToolTip("Haz clic en las teclas o escribe para ver los efectos que reaccionan al pulsar.")
        self._timer = QTimer(self, interval=33, timeout=self.update)

    def showEvent(self, event):
        self._timer.start()

    def hideEvent(self, event):
        self._timer.stop()

    def set_state(self, led: LedParam, backlight: bool) -> None:
        self.led, self.backlight = led, backlight
        self.update()

    def press(self, index: int) -> None:
        _, x, y, w = TH80_KEYS[index]
        now = time.monotonic()
        self.ripples.append((x + w / 2, y + 0.5, now))
        self.pressed[index] = now

    def _geometry(self) -> tuple[float, QPointF]:
        unit = min((self.width() - 32) / LAYOUT_W, (self.height() - 32) / LAYOUT_H)
        origin = QPointF((self.width() - unit * LAYOUT_W) / 2, (self.height() - unit * LAYOUT_H) / 2)
        return unit, origin

    def mousePressEvent(self, event):
        unit, origin = self._geometry()
        px = (event.position().x() - origin.x()) / unit
        py = (event.position().y() - origin.y()) / unit
        for i, (_, x, y, w) in enumerate(TH80_KEYS):
            if x <= px <= x + w and y <= py <= y + 1:
                self.press(i)
                return

    def _base(self, x: float, shift: float = 0.0) -> QColor:
        if self.led.rainbow:
            return hue(x / LAYOUT_W + shift)
        return QColor(self.led.r, self.led.g, self.led.b)

    def _light(self, i: int, x: float, y: float, t: float) -> tuple[QColor, float]:
        """Colour and intensity 0..1 of the LED under key `i` at time `t`."""
        led = self.led
        speed = 0.35 + led.speed * 0.4
        mode = led.mode
        if mode == MODES["respiracion"]:
            phase = t * speed * 2
            k = 0.1 + 0.9 * (0.5 + 0.5 * math.sin(phase))
            if led.rainbow:
                # The whole board breathes one colour and moves to the next one
                # at each dark point of the cycle.
                breath = int((phase + math.pi / 2) // (2 * math.pi))
                return QColor(*PRESET_COLORS[breath % len(PRESET_COLORS)]), k
            return self._base(x), k
        if mode == MODES["espectro"]:
            return hue(t * 0.08 * speed), 1.0
        if mode == MODES["onda"]:
            coord = [x, LAYOUT_W - x, y * 2.5, (LAYOUT_H - y) * 2.5][led.option % 4]
            if led.rainbow:
                return hue(coord / LAYOUT_W - t * 0.15 * speed), 1.0
            return self._base(x), 0.25 + 0.75 * (0.5 + 0.5 * math.sin(coord * 0.7 - t * speed * 3))
        if mode == MODES["ondulacion"]:
            best, color = 0.0, self._base(x)
            for rx, ry, start in self.ripples:
                age = t - start
                d = math.hypot(x - rx, (y - ry) * 1.1)
                band = max(0.0, 1 - abs(d - age * speed * 7) / 0.9) * max(0.0, 1 - age / 2.5)
                if band > best:
                    best = band
                    color = hue(start * 0.37 + d / 6) if led.rainbow else self._base(x)
            return color, best
        if mode == MODES["sombra"]:
            start = self.pressed.get(i)
            k = 0.0 if start is None else max(0.0, 1 - (t - start) * speed / 1.6)
            return self._base(x), k
        if mode == MODES["estrellas"]:
            twinkle = 0.5 + 0.5 * math.sin(t * speed * 3 + i * 2.39)
            return self._base(x, t * 0.03), twinkle**6
        return self._base(x), 1.0

    def paintEvent(self, event):
        now = time.monotonic()
        self.ripples = [r for r in self.ripples if now - r[2] < 2.5]
        unit, origin = self._geometry()
        level = (self.led.brightness + 1) / 5

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        body = QRectF(origin.x() - 12, origin.y() - 12, unit * LAYOUT_W + 24, unit * LAYOUT_H + 24)
        p.setPen(Qt.NoPen)
        p.setBrush(self.BODY)
        p.drawRoundedRect(body, 14, 14)

        font = QFont()
        font.setPixelSize(max(8, int(unit * 0.26)))
        p.setFont(font)
        pad = unit * 0.06
        for i, (label, x, y, w) in enumerate(TH80_KEYS):
            rect = QRectF(origin.x() + x * unit + pad, origin.y() + y * unit + pad, w * unit - 2 * pad, unit - 2 * pad)
            color, k = self._light(i, x + w / 2, y + 0.5, now) if self.backlight else (QColor(), 0.0)
            k *= level
            if k > 0.02:
                glow = QColor(color)
                glow.setAlphaF(min(1.0, 0.55 * k))
                p.setBrush(glow)
                p.drawRoundedRect(rect.adjusted(-pad, -pad, pad, pad), unit * 0.16, unit * 0.16)
            p.setBrush(mix(self.CAP, color, k * 0.55) if k > 0.02 else self.CAP)
            p.drawRoundedRect(rect, unit * 0.12, unit * 0.12)
            if label:
                p.setPen(QColor(235, 235, 240, 210))
                p.drawText(rect, Qt.AlignCenter, label)
                p.setPen(Qt.NoPen)

        kx, ky = origin.x() + KNOB[0] * unit, origin.y() + KNOB[1] * unit
        p.setBrush(QColor("#c8283c"))
        p.drawEllipse(QPointF(kx, ky), unit * 0.42, unit * 0.42)
        p.end()


# ---------------------------------------------------------------- worker

class _Bridge(QObject):
    """Carries results from the worker thread back to the GUI thread."""

    finished = Signal(object, object)
    failed = Signal(str)


class KeyboardWorker:
    """Runs keyboard I/O off the GUI thread, one job at a time, each in its own session.

    A fresh open per job matters: on firmware 2.01 a read in the same session
    right after a write stalls the settings channel.
    """

    def __init__(self, on_error):
        self._pool = ThreadPoolExecutor(max_workers=1)
        self._bridge = _Bridge()
        self._bridge.finished.connect(lambda callback, value: callback and callback(value))
        self._bridge.failed.connect(on_error)

    def submit(self, job, callback=None) -> None:
        def run():
            try:
                with Keyboard() as kb:
                    value = job(kb)
            except KeyboardError as e:
                self._bridge.failed.emit(str(e))
            except Exception as e:  # noqa: BLE001 - surface anything in the status bar
                self._bridge.failed.emit(f"Error inesperado: {e}")
            else:
                self._bridge.finished.emit(callback, value)

        self._pool.submit(run)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=True, cancel_futures=True)


# ---------------------------------------------------------------- window

def _slider() -> QSlider:
    s = QSlider(Qt.Horizontal)
    s.setRange(0, 4)
    s.setPageStep(1)
    s.setTickPosition(QSlider.TicksBelow)
    s.setTickInterval(1)
    s.setMinimumWidth(220)
    return s


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("kblinkboard · TH80")
        self.color = QColor(0, 128, 255)
        self.backlight = True
        self._loading = False

        self.worker = KeyboardWorker(self._on_error)
        self.apply_timer = QTimer(self, singleShot=True, interval=APPLY_DELAY_MS, timeout=self._send)

        self.preview = KeyboardPreview()

        self.mode_box = QComboBox()
        for name, number in MODES.items():
            self.mode_box.addItem(MODE_LABELS[name], number)

        self.color_button = QPushButton()
        self.color_button.setFixedSize(64, 26)
        self.color_button.clicked.connect(self._pick_color)
        self.rainbow_box = QCheckBox("Arcoíris")
        color_row = QHBoxLayout()
        color_row.addWidget(self.color_button)
        color_row.addWidget(self.rainbow_box)
        color_row.addStretch()

        self.option_box = QComboBox()
        self.brightness = _slider()
        self.speed = _slider()

        controls = QFormLayout()
        controls.addRow("Efecto", self.mode_box)
        controls.addRow("Color", color_row)
        controls.addRow("Dirección", self.option_box)
        controls.addRow("Brillo", self.brightness)
        controls.addRow("Velocidad", self.speed)
        self.option_label = controls.labelForField(self.option_box)
        settings = QGroupBox("Luces")
        settings.setLayout(controls)

        presets = QGroupBox("Rápidos")
        grid = QGridLayout(presets)
        for n, preset in enumerate(PRESETS):
            b = QPushButton(preset.name)
            b.clicked.connect(lambda _=False, p=preset: self.apply_preset(p))
            grid.addWidget(b, n // 2, n % 2)

        self.power_button = QPushButton("Apagar luces")
        self.power_button.clicked.connect(lambda: self.set_backlight(not self.backlight))
        self.read_button = QPushButton("Leer del teclado")
        self.read_button.clicked.connect(self.load_from_keyboard)
        actions = QHBoxLayout()
        actions.addWidget(self.power_button)
        actions.addWidget(self.read_button)
        actions.addStretch()

        side = QVBoxLayout()
        side.addWidget(settings)
        side.addWidget(presets)
        side.addLayout(actions)
        side.addStretch()

        root = QHBoxLayout()
        root.addWidget(self.preview, 3)
        root.addLayout(side, 2)
        central = QWidget()
        central.setLayout(root)
        self.setCentralWidget(central)
        self.statusBar().showMessage("Conectando con el teclado…")

        self.mode_box.currentIndexChanged.connect(self._mode_changed)
        self.rainbow_box.toggled.connect(self._changed)
        self.option_box.currentIndexChanged.connect(self._changed)
        self.brightness.valueChanged.connect(self._changed)
        self.speed.valueChanged.connect(self._changed)

        self._set_controls(LedParam(1, 2, 3, 0, False, 0, 128, 255))
        self.resize(1100, 420)
        QApplication.instance().installEventFilter(self)
        self._last_key_event = None
        self.load_from_keyboard()

    # -- state <-> controls

    def current_led(self) -> LedParam:
        return LedParam(
            mode=self.mode_box.currentData(),
            speed=self.speed.value(),
            brightness=self.brightness.value(),
            option=max(0, self.option_box.currentIndex()),
            rainbow=self.rainbow_box.isChecked(),
            r=self.color.red(),
            g=self.color.green(),
            b=self.color.blue(),
        )

    def _set_controls(self, led: LedParam) -> None:
        self._loading = True
        index = self.mode_box.findData(led.mode)
        self.mode_box.setCurrentIndex(max(0, index))
        self._fill_options(led.mode, led.option)
        self.rainbow_box.setChecked(led.rainbow)
        self.brightness.setValue(led.brightness)
        self.speed.setValue(led.speed)
        self.color = QColor(led.r, led.g, led.b)
        self._loading = False
        self._refresh()

    def _fill_options(self, mode: int, selected: int = 0) -> None:
        count = MODE_OPTIONS.get(mode, 0)
        self.option_box.blockSignals(True)
        self.option_box.clear()
        self.option_box.addItems([f"Dirección {n + 1}" for n in range(count)])
        self.option_box.setCurrentIndex(min(selected, count - 1) if count else -1)
        self.option_box.blockSignals(False)
        self.option_box.setVisible(bool(count))
        self.option_label.setVisible(bool(count))

    def _refresh(self) -> None:
        self.color_button.setStyleSheet(
            f"background-color: {self.color.name()}; border: 1px solid #888; border-radius: 6px;"
        )
        self.color_button.setEnabled(not self.rainbow_box.isChecked())
        self.power_button.setText("Apagar luces" if self.backlight else "Encender luces")
        self.preview.set_state(self.current_led(), self.backlight)

    # -- changes

    def _mode_changed(self) -> None:
        self._fill_options(self.mode_box.currentData())
        self._changed()

    def _changed(self) -> None:
        self._refresh()
        if not self._loading:
            self.apply_timer.start()

    def _pick_color(self) -> None:
        color = QColorDialog.getColor(self.color, self, "Color de las luces")
        if color.isValid():
            self.color = color
            self._changed()

    def apply_preset(self, preset: Preset) -> None:
        self._loading = True
        self.mode_box.setCurrentIndex(self.mode_box.findData(MODES[preset.mode]))
        self._fill_options(MODES[preset.mode])
        self.rainbow_box.setChecked(preset.rainbow)
        self.color = QColor(*preset.color)
        self.speed.setValue(preset.speed)
        self._loading = False
        self._changed()

    # -- keyboard I/O

    def _send(self) -> None:
        led = self.current_led()
        self.statusBar().showMessage("Enviando…")
        self.worker.submit(lambda kb: kb.set_led(led), lambda _: self._sent(led))

    def _sent(self, led: LedParam) -> None:
        color = "arcoíris" if led.rainbow else f"#{led.hex_color}"
        self.statusBar().showMessage(f"Enviado: {MODE_LABELS.get(led.mode_name, led.mode_name)} · {color}", 4000)

    def load_from_keyboard(self) -> None:
        self.statusBar().showMessage("Leyendo el teclado…")
        self.worker.submit(lambda kb: (kb.get_led(), kb.backlight_on()), self._loaded)

    def _loaded(self, result) -> None:
        led, backlight = result
        self.backlight = backlight
        self._set_controls(led)
        self.statusBar().showMessage("Conectado al TH80", 4000)

    def set_backlight(self, on: bool) -> None:
        self.statusBar().showMessage("Enviando…")
        self.worker.submit(lambda kb: kb.set_backlight(on), lambda _: self._backlight_set(on))

    def _backlight_set(self, on: bool) -> None:
        self.backlight = on
        self._refresh()
        self.statusBar().showMessage("Luces encendidas" if on else "Luces apagadas", 4000)

    def _on_error(self, message: str) -> None:
        self.statusBar().showMessage(message)

    # -- typing on the real keyboard animates the preview

    def eventFilter(self, obj, event):
        if event.type() == QEvent.KeyPress and not event.isAutoRepeat():
            # The same event is offered again to each parent it propagates to.
            stamp = (event.timestamp(), event.key())
            if stamp != self._last_key_event:
                self._last_key_event = stamp
                index = key_for_event(event)
                if index is not None:
                    self.preview.press(index)
        return False


# ---------------------------------------------------------------- menu bar

def tray_icon() -> QIcon:
    pix = QPixmap(44, 44)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(Qt.black, 3))
    p.setBrush(Qt.NoBrush)
    p.drawRoundedRect(QRectF(3, 11, 38, 23), 5, 5)
    p.setPen(Qt.NoPen)
    p.setBrush(Qt.black)
    for row, y in enumerate((16, 21)):
        for col in range(6):
            p.drawRect(QRectF(8 + col * 5 + (row * 2), y, 3, 3))
    p.drawRect(QRectF(13, 27, 18, 3))
    p.end()
    icon = QIcon(pix)
    icon.setIsMask(True)  # template image: follows light/dark menu bar
    return icon


def build_tray(app: QApplication, window: MainWindow) -> QSystemTrayIcon:
    tray = QSystemTrayIcon(tray_icon(), app)
    tray.setToolTip("kblinkboard")
    menu = QMenu()
    for preset in PRESETS:
        action = QAction(preset.name, menu)
        action.triggered.connect(lambda _=False, p=preset: window.apply_preset(p))
        menu.addAction(action)
    menu.addSeparator()
    power = menu.addAction("Apagar / encender luces")
    power.triggered.connect(lambda: window.set_backlight(not window.backlight))
    show = menu.addAction("Abrir ventana")
    show.triggered.connect(lambda: (window.show(), window.raise_(), window.activateWindow()))
    menu.addSeparator()
    menu.addAction("Salir").triggered.connect(app.quit)
    tray.setContextMenu(menu)
    tray._menu = menu  # keep a Python reference
    tray.show()
    return tray


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("kblinkboard")
    window = MainWindow()
    if QSystemTrayIcon.isSystemTrayAvailable():
        # Closing the window only hides it; the app stays in the menu bar.
        app.setQuitOnLastWindowClosed(False)
        window.tray = build_tray(app, window)
    app.aboutToQuit.connect(window.worker.shutdown)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
