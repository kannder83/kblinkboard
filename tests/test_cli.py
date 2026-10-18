import pytest
from typer.testing import CliRunner

from kblinkboard.cli import app
from kblinkboard.device import Keyboard, KeyboardError, SET_LEDPARAM, SET_OPTIONS

runner = CliRunner()


def last_led_write(board):
    writes = [w for w in board.writes() if w[0] == SET_LEDPARAM]
    assert writes, "no light packet was sent"
    return writes[-1]


def test_info(connect):
    result = runner.invoke(app, ["info"])
    assert result.exit_code == 0
    assert "ID de dispositivo: 369" in result.output
    assert "Firmware: 2.01" in result.output
    assert "modo=ondulacion" in result.output
    assert connect.writes() == []  # info only reads


def test_modos_lists_modes_without_keyboard():
    result = runner.invoke(app, ["modos"])
    assert result.exit_code == 0
    assert "ondulacion" in result.output
    assert " 13 " not in result.output


def test_color_sets_static_single_colour(connect):
    result = runner.invoke(app, ["color", "ff0080", "--brillo", "3"])
    assert result.exit_code == 0, result.output
    assert last_led_write(connect)[1:8] == [1, 2, 3, 0x08, 0xFF, 0x00, 0x80]
    assert "Enviado" in result.output


def test_color_accepts_hash_prefix(connect):
    assert runner.invoke(app, ["color", "#00ff00"]).exit_code == 0
    assert last_led_write(connect)[5:8] == [0, 255, 0]


def test_color_keeps_current_brightness(connect):
    runner.invoke(app, ["color", "00ff00"])
    assert last_led_write(connect)[3] == 4  # FakeBoard's current brightness


@pytest.mark.parametrize("value", ["zz", "12345", "gg0000"])
def test_color_rejects_invalid_hex(connect, value):
    result = runner.invoke(app, ["color", value])
    assert result.exit_code == 2
    assert connect.writes() == []


def test_invertir_flips_the_colour_flag(connect):
    runner.invoke(app, ["--invertir", "color", "ff0000"])
    assert last_led_write(connect)[4] == 0x07


def test_modo_rainbow(connect):
    result = runner.invoke(app, ["modo", "ondulacion", "--arcoiris", "-v", "4"])
    assert result.exit_code == 0, result.output
    sent = last_led_write(connect)
    assert sent[1:5] == [5, 1, 4, 0x07]


def test_modo_with_colour_and_direction(connect):
    runner.invoke(app, ["modo", "onda", "-c", "00ff00", "-o", "3"])
    sent = last_led_write(connect)
    assert sent[1] == 4
    assert sent[4] == 0x38
    assert sent[5:8] == [0, 255, 0]


def test_modo_keeps_current_colour(connect):
    runner.invoke(app, ["modo", "respiracion"])
    assert last_led_write(connect)[5:8] == [255, 0, 0]  # FakeBoard's current colour


def test_modo_unknown(connect):
    result = runner.invoke(app, ["modo", "disco"])
    assert result.exit_code == 2
    assert connect.writes() == []


@pytest.mark.parametrize("args", [["modo", "onda", "-v", "5"], ["brillo", "7"], ["color", "ff0000", "-b", "-1"]])
def test_out_of_range_values(connect, args):
    assert runner.invoke(app, args).exit_code == 2
    assert connect.writes() == []


def test_brillo_keeps_the_rest(connect):
    runner.invoke(app, ["brillo", "1"])
    assert last_led_write(connect)[1:8] == [5, 2, 1, 0x08, 255, 0, 0]


def test_apagar_and_encender(connect):
    assert runner.invoke(app, ["apagar"]).exit_code == 0
    assert runner.invoke(app, ["encender"]).exit_code == 0
    flags = [w[2] for w in connect.writes() if w[0] == SET_OPTIONS]
    assert flags == [0x10, 0x00]


def test_keyboard_errors_exit_cleanly(monkeypatch):
    def fail(self):
        raise KeyboardError("No encuentro el TH80 (3151:4002). Conéctalo por cable USB.")

    monkeypatch.setattr(Keyboard, "open", fail)
    result = runner.invoke(app, ["info"])
    assert result.exit_code == 1
    assert "No encuentro el TH80" in result.output
