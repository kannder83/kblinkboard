import pytest

from kblinkboard import device
from kblinkboard.device import (
    GET_OPTIONS,
    SET_LEDPARAM,
    SET_OPTIONS,
    STALLED,
    Keyboard,
    KeyboardError,
    LedParam,
    checksum,
    packet,
)

from .conftest import FakeBoard


def led(**changes) -> LedParam:
    base = dict(mode=1, speed=2, brightness=3, option=0, rainbow=False, r=0, g=128, b=255)
    return LedParam(**{**base, **changes})


def expected_ck8(buf):
    return 0xFF - (sum(buf[:8]) & 0xFF)


# ---------------------------------------------------------------- packets

def test_packet_is_64_bytes_with_bit7_checksum():
    p = packet(0x8F)
    assert len(p) == 64
    assert p[0] == 0x8F
    assert p[7] == 0xFF - 0x8F


def test_bit8_checksum_covers_bytes_0_to_7():
    p = packet(SET_LEDPARAM, [1, 2, 3, 4, 5, 6, 7], bit8=True)
    assert p[8] == 0xFF - (sum([SET_LEDPARAM, 1, 2, 3, 4, 5, 6, 7]) & 0xFF)


def test_checksum_wraps_at_one_byte():
    buf = [0xFF] * 9
    checksum(buf, 8)
    assert buf[8] == 0xFF - ((0xFF * 8) & 0xFF)


# ---------------------------------------------------------------- set_led

def test_set_led_encodes_th80_packet(keyboard, board):
    keyboard.set_led(led(mode=5, speed=3, brightness=4, r=255, g=0, b=128))
    (sent,) = board.writes()
    # op, mode, wire speed (5 - 3), brightness, flags (8 = fixed colour on the TH80), R, G, B
    assert sent[:8] == [SET_LEDPARAM, 5, 2, 4, 0x08, 255, 0, 128]
    assert sent[8] == expected_ck8(sent)


def test_rainbow_uses_flag_7_on_th80(keyboard, board):
    keyboard.set_led(led(rainbow=True))
    assert board.writes()[0][4] == 0x07


def test_swapped_flags_for_other_firmwares(board):
    kb = Keyboard(swapped_flags=True)
    kb._dev = board
    kb.set_led(led(rainbow=False))
    kb.set_led(led(rainbow=True))
    assert [w[4] for w in board.writes()] == [0x07, 0x08]


def test_option_goes_in_high_nibble(keyboard, board):
    keyboard.set_led(led(mode=4, option=2))
    assert board.writes()[0][4] == 0x28


def test_white_is_sent_as_fafafa(keyboard, board):
    keyboard.set_led(led(r=255, g=255, b=255))
    assert board.writes()[0][5:8] == [0xFA, 0xFA, 0xFA]


@pytest.mark.parametrize(("speed", "wire"), [(0, 5), (4, 1), (9, 1), (-3, 5)])
def test_speed_is_inverted_and_clamped(keyboard, board, speed, wire):
    keyboard.set_led(led(speed=speed))
    assert board.writes()[0][2] == wire


def test_brightness_is_clamped(keyboard, board):
    keyboard.set_led(led(brightness=9))
    assert board.writes()[0][3] == 4


@pytest.mark.parametrize("mode", [0, 13, 20, 21, 22, 25, 99])
def test_unsupported_modes_are_never_sent(keyboard, board, mode):
    with pytest.raises(KeyboardError):
        keyboard.set_led(led(mode=mode))
    assert board.sent == []


def test_set_led_does_not_read_back(keyboard, board):
    # A read right after the write stalls the TH80's settings channel.
    keyboard.set_led(led())
    assert board.reads == 0
    assert len(board.sent) == 1


# ---------------------------------------------------------------- get_led

def test_get_led_parses_reply():
    board = FakeBoard(led=(4, 4, 2, 0x18, 10, 20, 30))
    kb = Keyboard()
    kb._dev = board
    assert kb.get_led() == LedParam(mode=4, speed=1, brightness=2, option=1, rainbow=False, r=10, g=20, b=30)


def test_get_led_reads_rainbow_flag():
    kb = Keyboard()
    kb._dev = FakeBoard(led=(5, 2, 4, 0x07, 255, 0, 0))
    assert kb.get_led().rainbow is True


def test_get_led_maps_fafafa_back_to_white():
    kb = Keyboard()
    kb._dev = FakeBoard(led=(1, 2, 4, 0x08, 0xFA, 0xFA, 0xFA))
    assert kb.get_led().hex_color == "ffffff"


def test_get_led_preset_index_overrides_rgb():
    kb = Keyboard()
    kb._dev = FakeBoard(led=(1, 2, 4, 0x03, 1, 2, 3))  # preset 3 = green
    assert (kb.get_led().r, kb.get_led().g, kb.get_led().b) == (0, 255, 0)


def test_mode_name():
    assert led(mode=5).mode_name == "ondulacion"
    assert led(mode=42).mode_name == "desconocido (42)"


# ---------------------------------------------------------------- queries

def test_identify_and_revision(keyboard):
    assert keyboard.identify() == 369
    assert keyboard.revision() == "2.01"


def test_query_retries_until_the_opcode_matches(keyboard, board):
    replies = iter([[0] * 64, [0x11] + [0] * 63, None])
    original = board.get_feature_report

    def flaky(report_id, length):
        reply = next(replies)
        return original(report_id, length) if reply is None else reply

    board.get_feature_report = flaky
    assert keyboard.identify() == 369


def test_query_gives_up(keyboard, board):
    board.get_feature_report = lambda report_id, length: [0] * 64
    with pytest.raises(KeyboardError, match="no respondió"):
        keyboard.identify()


def test_read_accepts_reply_without_report_id(keyboard, board):
    board.get_feature_report = lambda report_id, length: board.pending
    assert keyboard.identify() == 369


def test_stalled_channel_is_reported(keyboard, board):
    def fail(*args):
        raise OSError("read error")

    board.get_feature_report = fail
    with pytest.raises(KeyboardError) as error:
        keyboard.identify()
    assert str(error.value) == STALLED


def test_failed_send_is_reported(keyboard, board):
    def fail(*args):
        raise OSError("write error")

    board.send_feature_report = fail
    with pytest.raises(KeyboardError, match="no responde"):
        keyboard.set_led(led())


def test_writes_are_spaced(monkeypatch, keyboard):
    waits = []
    monkeypatch.setattr(device.time, "sleep", waits.append)
    keyboard.set_led(led())
    keyboard.set_led(led())
    assert any(0 < w <= device.MIN_WRITE_GAP for w in waits)


# ---------------------------------------------------------------- backlight

def test_backlight_off_keeps_other_option_bits():
    board = FakeBoard(options=(0x09, 0x02, 0x03))  # Win lock + WASD swap
    kb = Keyboard()
    kb._dev = board
    kb.set_backlight(False)
    (sent,) = board.writes()
    assert sent[0] == SET_OPTIONS
    assert sent[1:5] == [0, 0x19, 0x02, 0x03]
    assert board.sent[0][0] == GET_OPTIONS  # read-modify-write


def test_backlight_on_clears_only_its_bit():
    kb = Keyboard()
    kb._dev = board = FakeBoard(options=(0x19, 0, 0))
    kb.set_backlight(True)
    assert board.writes()[0][2] == 0x09


def test_backlight_on_reports_state():
    kb = Keyboard()
    kb._dev = FakeBoard(options=(0x10, 0, 0))
    assert kb.backlight_on() is False


# ---------------------------------------------------------------- open

def _hid_entry(interface, usage_page, usage):
    return {"interface_number": interface, "usage_page": usage_page, "usage": usage, "path": b"p%d" % interface}


def test_open_without_keyboard(monkeypatch):
    monkeypatch.setattr(device.hid, "enumerate", lambda vid, pid: [])
    with pytest.raises(KeyboardError, match="No encuentro"):
        Keyboard().open()


def test_open_ignores_other_interfaces(monkeypatch):
    monkeypatch.setattr(device.hid, "enumerate", lambda vid, pid: [_hid_entry(1, 0xFFFF, 1), _hid_entry(1, 0x01, 6)])
    with pytest.raises(KeyboardError, match="No encuentro"):
        Keyboard().open()


def test_open_uses_interface_0_keyboard_collection(monkeypatch):
    opened = []

    class Device:
        def open_path(self, path):
            opened.append(path)

        def close(self):
            pass

    monkeypatch.setattr(device.hid, "enumerate", lambda vid, pid: [_hid_entry(1, 0x01, 6), _hid_entry(0, 0x01, 6)])
    monkeypatch.setattr(device.hid, "device", Device)
    monkeypatch.setattr(device, "_set_non_exclusive", lambda: opened.append("non-exclusive"))
    with Keyboard():
        pass
    assert opened == ["non-exclusive", b"p0"]


def test_open_without_permission(monkeypatch):
    class Device:
        def open_path(self, path):
            raise OSError("open failed")

    monkeypatch.setattr(device.hid, "enumerate", lambda vid, pid: [_hid_entry(0, 0x01, 6)])
    monkeypatch.setattr(device.hid, "device", Device)
    monkeypatch.setattr(device, "_set_non_exclusive", lambda: None)
    with pytest.raises(KeyboardError, match="Monitorización de entrada"):
        Keyboard().open()


def test_close_releases_device(keyboard, board):
    keyboard.close()
    assert board.closed
    keyboard.close()  # closing twice is harmless
