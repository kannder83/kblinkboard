"""ROYUAN (yc500) HID protocol for the Epomaker TH80 (USB 3151:4002).

Protocol reference: https://github.com/dniminenn/sharkfin/blob/master/docs/PROTOCOL.md
64-byte feature reports, report ID 0. On 3151:4002 the settings channel is
interface 0, keyboard collection (usage page 0x01, usage 0x06).
"""

import ctypes
import time
from dataclasses import dataclass

import hid

VENDOR_ID = 0x3151
PRODUCT_ID = 0x4002
REPORT_LEN = 64

# Opcodes (yc500 family)
GET_IDENTIFY = 0x8F
GET_REVISION = 0x80
SET_LEDPARAM = 0x07
GET_LEDPARAM = 0x87
SET_OPTIONS = 0x06
GET_OPTIONS = 0x86

OPTION_BACKLIGHT_OFF = 0x10

# Minimum gap between writes; faster stalls the endpoint until replug.
MIN_WRITE_GAP = 0.012
REPLY_TRIES = 5

SPEED_WIRE_MAX = 5  # yc500 speed on the wire is 1..5, inverted from the 0..4 scale
BRIGHTNESS_MAX = 4

# Wire mode numbers. 13, 20, 21, 22 and 25 need host-side data and are left out.
MODES = {
    "estatico": 1,
    "respiracion": 2,
    "espectro": 3,
    "onda": 4,
    "ondulacion": 5,
    "estrellas": 6,
    "flujo": 7,
    "sombra": 8,
    "capas": 9,
    "seno": 10,
    "resorte": 11,
    "neon": 12,
    "radiante": 14,
    "bucle": 15,
    "cuadricula": 16,
    "nevada": 17,
    "meteoro": 18,
    "nieve": 19,
}
MODE_NAMES = {v: k for k, v in MODES.items()}

# Modes whose option nibble selects a direction or variant, and how many there are.
MODE_OPTIONS = {4: 4, 7: 2, 11: 2, 12: 2, 15: 2}

# On GET, a flags nibble of 0..6 is a preset colour index that overrides RGB.
PRESET_COLORS = [
    (0xFF, 0x00, 0x00),
    (0xFF, 0x80, 0x00),
    (0xFF, 0xFF, 0x00),
    (0x00, 0xFF, 0x00),
    (0x00, 0xFF, 0xFF),
    (0x00, 0x00, 0xFF),
    (0xFF, 0x00, 0xFF),
]


class KeyboardError(Exception):
    pass


# A stalled endpoint fails every report until the board re-enumerates.
STALLED = (
    "El canal de ajustes del teclado no responde. "
    "Desconecta y vuelve a conectar el cable USB, y repite el comando."
)

# Pause after SET_LEDPARAM before closing, as sharkfin's restore_light does.
LED_SETTLE = 0.5


def checksum(buf: list[int], upto: int) -> None:
    """0xFF - (sum of bytes 0..upto-1), stored at byte `upto` (Bit7 = 7, Bit8 = 8)."""
    buf[upto] = 0xFF - (sum(buf[:upto]) & 0xFF)


def packet(opcode: int, payload: list[int] = (), bit8: bool = False) -> list[int]:
    buf = [0] * REPORT_LEN
    buf[0] = opcode
    buf[1 : 1 + len(payload)] = payload
    checksum(buf, 8 if bit8 else 7)
    return buf


@dataclass
class LedParam:
    mode: int
    speed: int  # 0..4, 4 is fastest
    brightness: int  # 0..4
    option: int  # direction / variant, 0..15
    rainbow: bool
    r: int
    g: int
    b: int

    @property
    def mode_name(self) -> str:
        return MODE_NAMES.get(self.mode, f"desconocido ({self.mode})")

    @property
    def hex_color(self) -> str:
        return f"{self.r:02x}{self.g:02x}{self.b:02x}"


def _set_non_exclusive() -> None:
    """hidapi on macOS seizes devices by default, which a keyboard refuses.

    The cython `hidapi` module links hidapi statically and exports the
    darwin helper, so it is reached through ctypes. hid_init (run by the
    first enumerate) resets it, so call this after enumerating.
    """
    try:
        lib = ctypes.CDLL(hid.__file__)
        lib.hid_darwin_set_open_exclusive(0)
    except (OSError, AttributeError):
        pass


class Keyboard:
    def __init__(self, swapped_flags: bool = False):
        # The TH80 (firmware 2.01) reads flags nibble 8 as fixed colour and 7 as
        # rainbow, like the Akko 3098B on the same USB id. Most other ROYUAN
        # boards read them the other way round; `swapped_flags` selects that.
        self.fixed_flag, self.rainbow_flag = (7, 8) if swapped_flags else (8, 7)
        self._dev: hid.device | None = None
        self._last_write = 0.0

    def __enter__(self) -> "Keyboard":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def open(self) -> None:
        paths = [
            d["path"]
            for d in hid.enumerate(VENDOR_ID, PRODUCT_ID)
            if d["interface_number"] == 0 and d["usage_page"] == 0x01 and d["usage"] == 0x06
        ]
        if not paths:
            raise KeyboardError(
                "No encuentro el TH80 (3151:4002). Conéctalo por cable USB."
            )
        _set_non_exclusive()
        dev = hid.device()
        try:
            dev.open_path(paths[0])
        except OSError as e:
            raise KeyboardError(
                "No pude abrir el teclado. Da permiso de 'Monitorización de entrada' "
                "a tu terminal en Ajustes del Sistema > Privacidad y seguridad, "
                "y vuelve a abrirla."
            ) from e
        self._dev = dev

    def close(self) -> None:
        if self._dev is not None:
            self._dev.close()
            self._dev = None

    def _send(self, buf: list[int]) -> None:
        wait = MIN_WRITE_GAP - (time.monotonic() - self._last_write)
        if wait > 0:
            time.sleep(wait)
        try:
            self._dev.send_feature_report([0] + buf)
        except OSError as e:
            raise KeyboardError(STALLED) from e
        self._last_write = time.monotonic()

    def _read(self) -> list[int]:
        try:
            reply = self._dev.get_feature_report(0, REPORT_LEN + 1)
        except OSError as e:
            raise KeyboardError(STALLED) from e
        # Some platforms keep the report-ID byte in front.
        if len(reply) == REPORT_LEN + 1 and reply[0] == 0:
            reply = reply[1:]
        return list(reply)

    def query(self, opcode: int) -> list[int]:
        self._send(packet(opcode))
        for attempt in range(1, REPLY_TRIES + 1):
            time.sleep(0.01 * attempt)
            reply = self._read()
            if reply and reply[0] == opcode:
                return reply
        raise KeyboardError(f"El teclado no respondió al comando 0x{opcode:02X}.")

    def identify(self) -> int:
        r = self.query(GET_IDENTIFY)
        return int.from_bytes(bytes(r[1:5]), "little")

    def revision(self) -> str:
        r = self.query(GET_REVISION)
        return f"{r[2]:x}.{r[1]:02x}"

    def get_led(self) -> LedParam:
        r = self.query(GET_LEDPARAM)
        flags = r[4]
        nibble = flags & 0x0F
        rgb = (r[5], r[6], r[7])
        if rgb == (0xFA, 0xFA, 0xFA):
            rgb = (0xFF, 0xFF, 0xFF)
        rainbow = nibble == self.rainbow_flag
        if not rainbow and nibble != self.fixed_flag and nibble < len(PRESET_COLORS):
            rgb = PRESET_COLORS[nibble]
        return LedParam(
            mode=r[1],
            speed=max(0, min(4, SPEED_WIRE_MAX - r[2])),
            brightness=min(r[3], BRIGHTNESS_MAX),
            option=flags >> 4,
            rainbow=rainbow,
            r=rgb[0],
            g=rgb[1],
            b=rgb[2],
        )

    def set_led(self, led: LedParam) -> None:
        """Send the light setting.

        No read-back: on firmware 2.01 a GET right after SET_LEDPARAM fails and
        leaves the settings channel stalled until the cable is replugged.
        """
        if led.mode not in MODE_NAMES:
            raise KeyboardError(f"Modo no soportado: {led.mode}")
        r, g, b = led.r, led.g, led.b
        if (r, g, b) == (0xFF, 0xFF, 0xFF):
            r = g = b = 0xFA  # white is sent as FAFAFA
        flags = ((led.option & 0x0F) << 4) | (self.rainbow_flag if led.rainbow else self.fixed_flag)
        self._send(
            packet(
                SET_LEDPARAM,
                [
                    led.mode,
                    SPEED_WIRE_MAX - max(0, min(4, led.speed)),
                    max(0, min(BRIGHTNESS_MAX, led.brightness)),
                    flags,
                    r,
                    g,
                    b,
                ],
                bit8=True,
            )
        )
        time.sleep(LED_SETTLE)

    def set_backlight(self, on: bool) -> None:
        # Read-modify-write so other option bits (Win lock, keyboard lock...) are kept.
        r = self.query(GET_OPTIONS)
        flags = r[2] & ~OPTION_BACKLIGHT_OFF if on else r[2] | OPTION_BACKLIGHT_OFF
        self._send(packet(SET_OPTIONS, [0, flags, r[3], r[4]]))
        time.sleep(0.2)

    def backlight_on(self) -> bool:
        return not (self.query(GET_OPTIONS)[2] & OPTION_BACKLIGHT_OFF)
