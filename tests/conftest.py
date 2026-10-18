import os

# Qt must not open real windows while testing.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from kblinkboard import device
from kblinkboard.device import REPORT_LEN, Keyboard


class FakeBoard:
    """Stands in for a hid.device: answers GETs from its state and records every report."""

    def __init__(self, led=(5, 2, 4, 0x08, 255, 0, 0), options=(0x00, 0x00, 0x00), device_id=369, revision=(0x01, 0x02)):
        self.replies = {
            0x8F: [0x8F, *device_id.to_bytes(4, "little")],
            0x80: [0x80, *revision],
            0x87: [0x87, *led],
            0x86: [0x86, 0x00, *options],
        }
        self.sent: list[list[int]] = []
        self.reads = 0
        self.pending: list[int] | None = None
        self.closed = False

    def send_feature_report(self, data):
        assert data[0] == 0, "report ID 0 must lead every feature report"
        assert len(data) == REPORT_LEN + 1
        buf = list(data[1:])
        self.sent.append(buf)
        if buf[0] & 0x80:
            reply = self.replies[buf[0]]
            self.pending = reply + [0] * (REPORT_LEN - len(reply))

    def get_feature_report(self, report_id, length):
        self.reads += 1
        # macOS keeps the report-ID byte in front of the data.
        return [0] + (self.pending or [0] * REPORT_LEN)

    def close(self):
        self.closed = True

    def writes(self) -> list[list[int]]:
        return [p for p in self.sent if not p[0] & 0x80]


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(device.time, "sleep", lambda seconds: None)


@pytest.fixture
def board():
    return FakeBoard()


@pytest.fixture
def keyboard(board):
    kb = Keyboard()
    kb._dev = board
    return kb


@pytest.fixture
def connect(monkeypatch, board):
    """Make every Keyboard opened by the CLI or GUI talk to `board`."""

    def fake_open(self):
        self._dev = board

    monkeypatch.setattr(Keyboard, "open", fake_open)
    return board
