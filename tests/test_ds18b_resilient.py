"""Run in mqtt-io's venv; no services, GPIO or MQTT are started."""
import importlib.util
import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch

# Unit tests must never try to load kernel modules.
os.environ["W1THERMSENSOR_NO_KERNEL_MODULE"] = "1"

spec = importlib.util.spec_from_file_location(
    "mqtt_io.modules.sensor.ds18b_resilient", pathlib.Path(__file__).resolve().parents[1] / "ds18b_resilient.py")
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)
from w1thermsensor.errors import NoSensorFoundError


class DriverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sensor = driver.Sensor(dict(address="0123456789ab", type="DS18B20",
                                         w1_master=5, single_sensor=True))
        self.sensor.base = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.sensor.executor.shutdown(wait=True)
        self.tmp.cleanup()

    def bus(self, names):
        p = self.sensor.base / "w1_bus_master5"
        p.mkdir(exist_ok=True)
        (p / "w1_master_slaves").write_text("\n".join(names) or "not found.")
        (p / "w1_master_slave_count").write_text(str(len(names)))

    def test_absent_does_not_probe_at_startup(self):
        self.assertIsNone(self.sensor._mode)

    def test_normal_then_fallback_then_normal(self):
        with patch.object(self.sensor, "_normal_value", return_value=12.0), \
             patch.object(self.sensor, "_raw_value") as raw:
            self.assertEqual(self.sensor.get_value({}), 12.0)
            raw.assert_not_called()
        with patch.object(self.sensor, "_normal_value", side_effect=NoSensorFoundError("gone")), \
             patch.object(self.sensor, "_raw_value", return_value=(11.0, "skip_rom")):
            self.assertEqual(self.sensor.get_value({}), 11.0)
        with patch.object(self.sensor, "_normal_value", return_value=13.0), \
             patch.object(self.sensor, "_raw_value") as raw:
            self.assertEqual(self.sensor.get_value({}), 13.0)
            raw.assert_not_called()
        self.assertEqual(self.sensor._mode, "address")

    def test_failure_never_returns_previous_reading(self):
        with patch.object(self.sensor, "_normal_value", return_value=12.0):
            self.sensor.get_value({})
        with patch.object(self.sensor, "_normal_value", side_effect=NoSensorFoundError("gone")), \
             patch.object(self.sensor, "_raw_value", side_effect=driver.ReadingError("CRC")):
            with self.assertRaises(driver.ReadingError):
                self.sensor.get_value({})
        self.assertEqual(self.sensor._mode, "unavailable")

    def test_existing_sensor_read_error_does_not_broadcast(self):
        p = self.sensor.base / self.sensor.slave_name
        p.mkdir()
        (p / "w1_slave").touch()
        with patch.object(self.sensor, "_normal_value", side_effect=NoSensorFoundError("read error")), \
             patch.object(self.sensor, "_raw_value") as raw:
            with self.assertRaises(NoSensorFoundError):
                self.sensor.get_value({})
            raw.assert_not_called()

    def test_reject_other_sensor_and_multiple_sensors(self):
        for names in [["28-ffffffffffff"], [self.sensor.slave_name, "28-ffffffffffff"]]:
            self.bus(names)
            with self.assertRaises(driver.ReadingError):
                self.sensor._check_bus()
        self.bus([])
        self.sensor._check_bus()

    def test_reject_other_rom_before_conversion(self):
        self.bus([])
        with patch.object(driver, "transaction", return_value=b"\xff" * 8) as transact:
            with self.assertRaises(driver.ReadingError):
                self.sensor._raw_value()
            self.assertEqual(transact.call_count, 1)

    def test_fresh_raw_conversion_and_crc(self):
        self.bus([])
        with patch.object(driver, "transaction", side_effect=[bytes(8), b"", bytes.fromhex("b00000001f000000cc")]) as transact, \
             patch.object(driver.time, "sleep") as sleep:
            self.assertEqual(self.sensor._raw_value(), (11.0, "skip_rom"))
            self.assertEqual([x.args for x in transact.call_args_list],
                             [(5, b"\x33", 8), (5, b"\xcc\x44"), (5, b"\xcc\xbe", 9)])
            sleep.assert_called_once_with(1.0)

    def test_crc_empty_poweron_and_negative(self):
        valid = bytes.fromhex("b00000001f000000cc")
        self.assertEqual(driver.temperature_from_scratchpad(valid), 11.0)
        invalids = [bytes(9), b"\xff" * 9, valid[:-1] + b"\x00"]
        poweron = bytes.fromhex("500500007f000000")
        invalids.append(poweron + bytes([driver.crc8(poweron)]))
        for data in invalids:
            with self.assertRaises(driver.ReadingError):
                driver.temperature_from_scratchpad(data)
        negative = bytes.fromhex("58ff00007f000000")
        self.assertEqual(driver.temperature_from_scratchpad(
            negative + bytes([driver.crc8(negative)])), -10.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
