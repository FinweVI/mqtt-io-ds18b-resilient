"""DS18B20 readings with an opt-in, single-sensor SKIP_ROM fallback.

No probing during setup: a missing sensor must not prevent MQTT-IO starting.
The normal w1thermsensor path is retried at every poll. Raw I/O uses Linux's
w1 netlink connector, not GPIO bit banging, and leaves discovery running.
"""
import logging
import socket
import struct
import threading
import time
from pathlib import Path

from . import GenericSensor

REQUIREMENTS = ("w1thermsensor>=2.0.0",)
CONFIG_SCHEMA = {
    "address": dict(type="string", required=True, regex="[0-9a-fA-F]{12}"),
    "type": dict(type="string", required=True, allowed=["DS18B20", "ds18b20"]),
    "w1_master": dict(type="integer", required=True, min=1),
    "single_sensor": dict(type="boolean", required=True, allowed=[True]),
}
_LOG = logging.getLogger(__name__)


class ReadingError(RuntimeError):
    """No fresh, attributable reading can be supplied."""


def crc8(data):
    crc = 0
    for byte in data:
        for _ in range(8):
            bit = (crc ^ byte) & 1
            crc >>= 1
            if bit:
                crc ^= 0x8C
            byte >>= 1
    return crc


def temperature_from_scratchpad(data):
    if len(data) != 9 or data in (bytes(9), b"\xff" * 9):
        raise ReadingError("Empty or invalid scratchpad")
    if crc8(data[:8]) != data[8]:
        raise ReadingError("Scratchpad CRC mismatch")
    # Reserved bits of the DS18B20 configuration byte must have this layout.
    if data[4] & 0x9F != 0x1F:
        raise ReadingError("Unexpected DS18B20 configuration")
    raw = int.from_bytes(data[:2], "little", signed=True)
    # Mask undefined low bits for 9/10/11-bit conversions.
    undefined_bits = 3 - ((data[4] >> 5) & 3)
    raw &= ~((1 << undefined_bits) - 1)
    value = raw / 16.0
    if not -55.0 <= value <= 125.0 or value == 85.0:
        raise ReadingError("Out of range or power-on reset temperature")
    return value


def transaction(master, write, read_size=0, timeout=3.0):
    """Atomically RESET + WRITE + optional READ under the kernel bus mutex."""
    commands = struct.pack("<BBH", 5, 0, 0)
    commands += struct.pack("<BBH", 1, 0, len(write)) + write
    if read_size:
        commands += struct.pack("<BBH", 0, 0, read_size) + bytes(read_size)
    seq = time.monotonic_ns() & 0xFFFFFFFF
    w1 = struct.pack("<BBHII", 4, 0, len(commands), master, 0) + commands
    connector = struct.pack("<IIIIHH", 3, 1, seq, 0, len(w1), 0) + w1
    with socket.socket(socket.AF_NETLINK, socket.SOCK_DGRAM, 11) as sock:
        sock.bind((0, 0))
        port = sock.getsockname()[0]
        packet = struct.pack("<IHHII", 16 + len(connector), 3, 0, seq, port) + connector
        deadline = time.monotonic() + timeout
        sock.sendto(packet, (0, 0))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ReadingError("w1 netlink timed out")
            sock.settimeout(remaining)
            try:
                packet = sock.recv(16384)
            except socket.timeout as exc:
                raise ReadingError("w1 netlink timed out") from exc
            pos = 0
            while pos + 16 <= len(packet):
                length, kind, _, nlseq, _ = struct.unpack_from("<IHHII", packet, pos)
                if length < 16 or pos + length > len(packet):
                    raise ReadingError("Malformed netlink response")
                payload = packet[pos + 16:pos + length]
                pos += (length + 3) & ~3
                if kind == 2 and nlseq == seq:
                    if len(payload) < 4:
                        raise ReadingError("Truncated netlink error")
                    error = struct.unpack_from("<i", payload)[0]
                    if error:
                        raise ReadingError("Netlink error %d" % error)
                    continue
                if len(payload) < 20:
                    continue
                idx, val, cnseq, _, size, _ = struct.unpack_from("<IIIIHH", payload)
                if (idx, val, cnseq) != (3, 1, seq):
                    continue
                if size < 12 or 20 + size > len(payload):
                    raise ReadingError("Truncated w1 response")
                msg = payload[20:20 + size]
                mtype, status, mlen, bus, _ = struct.unpack_from("<BBHII", msg)
                if mtype != 4 or bus != master:
                    continue
                if status:
                    raise ReadingError("w1 command failed: %d" % status)
                if mlen < 4 or 12 + mlen > len(msg):
                    raise ReadingError("Malformed w1 command response")
                command, _, count = struct.unpack_from("<BBH", msg, 12)
                if read_size and command == 0 and count == read_size:
                    data = msg[16:16 + count]
                    if len(data) != count:
                        raise ReadingError("Truncated w1 data")
                    return data
                if not read_size and command == 1 and count == 0:
                    return b""


class Sensor(GenericSensor):
    def setup_module(self):
        self.address = self.config["address"].lower()
        self.master = self.config["w1_master"]
        if self.config["single_sensor"] is not True:
            raise ValueError("Raw fallback requires a dedicated single-sensor bus")
        self.base = Path("/sys/bus/w1/devices")
        self.slave_name = "28-" + self.address
        self._lock = threading.Lock()
        self._mode = None

    def _normal_value(self):
        from w1thermsensor import W1ThermSensor
        from w1thermsensor.sensors import Sensor as SensorType
        sensor = W1ThermSensor(SensorType.DS18B20, self.address)
        return sensor.get_temperature()

    def _check_bus(self):
        bus = self.base / ("w1_bus_master%d" % self.master)
        text = (bus / "w1_master_slaves").read_text().strip()
        slaves = [] if text in ("", "not found.") else text.splitlines()
        count = int((bus / "w1_master_slave_count").read_text().strip())
        if count != len(slaves) or any(s != self.slave_name for s in slaves):
            raise ReadingError("Bus is not empty or dedicated to the expected sensor")
        if count > 1:
            raise ReadingError("SKIP_ROM is forbidden on a bus with multiple sensors")

    def _raw_value(self):
        self._check_bus()
        rom = transaction(self.master, b"\x33", 8)
        # Known failure: all-zero identity. Also permit the historical identity
        # before sysfs discovery has caught up, but never a different sensor.
        expected = bytes.fromhex("28") + bytes.fromhex(self.address)[::-1]
        expected += bytes([crc8(expected)])
        if rom not in (bytes(8), expected):
            raise ReadingError("Unexpected physical identity on fallback bus")
        self._check_bus()
        transaction(self.master, b"\xcc\x44")
        time.sleep(1.0)  # Full conversion even for clones with unusual resolution.
        self._check_bus()
        if (self.base / self.slave_name / "w1_slave").exists():
            # Address reappeared during conversion; use the normal interface.
            return self._normal_value(), "address"
        data = transaction(self.master, b"\xcc\xbe", 9)
        return temperature_from_scratchpad(data), "skip_rom"

    def _set_mode(self, mode):
        if mode != self._mode:
            _LOG.info("Sensor %s: %s -> %s (w1 master %d)",
                      self.address, self._mode or "initial", mode, self.master)
            self._mode = mode

    def get_value(self, sens_conf):
        from w1thermsensor.errors import NoSensorFoundError
        with self._lock:
            try:
                try:
                    value = self._normal_value()
                    mode = "address"
                except NoSensorFoundError:
                    # A read error on a still-present sensor is not evidence of
                    # lost identity, so don't switch to broadcast for that case.
                    if (self.base / self.slave_name / "w1_slave").exists():
                        raise
                    value, mode = self._raw_value()
            except Exception as exc:
                if self._mode != "unavailable":
                    _LOG.warning("Sensor %s: reading failed (%s): %s",
                                 self.address, type(exc).__name__, exc)
                self._set_mode("unavailable")
                raise
            self._set_mode(mode)
            return value
