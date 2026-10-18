import time

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication

from kblinkboard import gui
from kblinkboard.device import MODES, SET_LEDPARAM, Keyboard, LedParam
from kblinkboard.gui import (
    LAYOUT_W,
    PRESETS,
    TH80_KEYS,
    _KEY_INDEX,
    KeyboardPreview,
    MainWindow,
    key_for_event,
)


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


def led(**changes) -> LedParam:
    base = dict(mode=1, speed=2, brightness=4, option=0, rainbow=False, r=255, g=0, b=0)
    return LedParam(**{**base, **changes})


# ---------------------------------------------------------------- layout

def test_th80_has_80_keys():
    assert len(TH80_KEYS) == 80


def test_rows_fit_and_keys_do_not_overlap():
    rows: dict[float, list] = {}
    for _, x, y, w in TH80_KEYS:
        rows.setdefault(y, []).append((x, w))
    for keys in rows.values():
        keys.sort()
        assert keys[-1][0] + keys[-1][1] <= LAYOUT_W
        for (x1, w1), (x2, _) in zip(keys, keys[1:]):
            assert x1 + w1 <= x2 + 1e-9


# ---------------------------------------------------------------- key events

def key_event(key, text=""):
    return QKeyEvent(QEvent.KeyPress, key, Qt.NoModifier, text)


@pytest.mark.parametrize(
    ("key", "text", "label"),
    [
        (Qt.Key_G, "g", "G"),
        (Qt.Key_1, "!", "1"),
        (Qt.Key_Space, " ", ""),
        (Qt.Key_Return, "\r", "Enter"),
        (Qt.Key_Control, "", "⌘"),  # Qt on macOS reports ⌘ as Control
        (Qt.Key_Meta, "", "Ctrl"),
        (Qt.Key_F12, "", "F12"),
        (Qt.Key_Up, "", "↑"),
    ],
)
def test_key_for_event(qapp, key, text, label):
    assert key_for_event(key_event(key, text)) == _KEY_INDEX[label]


def test_unknown_key_is_ignored(qapp):
    assert key_for_event(key_event(Qt.Key_VolumeUp)) is None


# ---------------------------------------------------------------- preview

def colours_across(preview, t):
    return {preview._light(0, x, 2.5, t)[0].name() for x in (1, 8, 15)}


def test_static_colour_is_uniform(qapp):
    p = KeyboardPreview()
    p.led = led()
    assert colours_across(p, 0) == {"#ff0000"}
    assert p._light(0, 1, 1, 0)[1] == 1.0


def test_static_rainbow_is_a_gradient(qapp):
    p = KeyboardPreview()
    p.led = led(rainbow=True)
    assert len(colours_across(p, 0)) == 3


def test_breathing_rainbow_is_one_colour_that_changes_per_breath(qapp):
    p = KeyboardPreview()
    p.led = led(mode=MODES["respiracion"], rainbow=True)
    seen = set()
    for t in [i * 0.25 for i in range(40)]:
        colours = colours_across(p, t)
        assert len(colours) == 1
        seen |= colours
    assert len(seen) > 1


def test_ripple_starts_dark_and_lights_up_after_a_press(qapp):
    p = KeyboardPreview()
    p.led = led(mode=MODES["ondulacion"])
    now = time.monotonic()
    assert p._light(0, 9, 3.5, now)[1] == 0
    p.press(_KEY_INDEX["G"])
    intensities = [p._light(0, 9, 3.5, now + dt)[1] for dt in (0.1, 0.2, 0.3, 0.5)]
    assert max(intensities) > 0.5


def test_key_shadow_fades(qapp):
    p = KeyboardPreview()
    p.led = led(mode=MODES["sombra"])
    i = _KEY_INDEX["A"]
    p.press(i)
    start = p.pressed[i]
    assert p._light(i, 0, 0, start)[1] == pytest.approx(1.0)
    assert p._light(i, 0, 0, start + 10)[1] == 0


def test_preview_paints(qapp):
    p = KeyboardPreview()
    p.resize(700, 280)
    for mode in MODES.values():
        p.set_state(led(mode=mode, rainbow=True), backlight=True)
        p.grab()
    p.set_state(led(), backlight=False)
    p.grab()


# ---------------------------------------------------------------- window

def test_presets_use_known_modes():
    assert all(preset.mode in MODES for preset in PRESETS)


class RecordingWorker:
    """Runs jobs right away against the fake board, on the GUI thread."""

    def __init__(self, on_error):
        self.on_error = on_error

    def submit(self, job, callback=None):
        with Keyboard() as kb:
            value = job(kb)
        if callback:
            callback(value)

    def shutdown(self):
        pass


@pytest.fixture
def window(qapp, connect, monkeypatch):
    monkeypatch.setattr(gui, "KeyboardWorker", RecordingWorker)
    w = MainWindow()
    yield w
    w.close()


def test_window_loads_state_from_keyboard_without_writing(window, connect):
    assert window.current_led().mode == 5
    assert window.brightness.value() == 4
    assert connect.writes() == []
    assert not window.apply_timer.isActive()


def test_changing_a_control_schedules_a_write(window, connect):
    window.speed.setValue(4)
    assert window.apply_timer.isActive()
    window._send()
    assert connect.writes()[-1][0] == SET_LEDPARAM
    assert connect.writes()[-1][2] == 1  # speed 4 -> wire 1


def test_preset_sends_its_effect(window, connect):
    window.apply_preset(next(p for p in PRESETS if p.name == "Ondulación arcoíris"))
    window._send()
    sent = connect.writes()[-1]
    assert sent[1] == MODES["ondulacion"]
    assert sent[4] & 0x0F == 0x07  # rainbow on the TH80


def test_direction_only_for_modes_that_have_it(window):
    window.mode_box.setCurrentIndex(window.mode_box.findData(MODES["onda"]))
    assert window.option_box.count() == 4
    window.mode_box.setCurrentIndex(window.mode_box.findData(MODES["estatico"]))
    assert window.option_box.count() == 0


def test_rainbow_disables_colour_button(window):
    window.rainbow_box.setChecked(True)
    assert not window.color_button.isEnabled()
    window.rainbow_box.setChecked(False)
    assert window.color_button.isEnabled()


def test_backlight_toggle(window, connect):
    window.set_backlight(False)
    assert window.power_button.text() == "Encender luces"
    window.set_backlight(True)
    assert window.power_button.text() == "Apagar luces"
