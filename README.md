# MQTT-IO DS18B20 resilient driver

A small custom sensor module for [MQTT-IO](https://github.com/flyte/mqtt-io).
It keeps reading a DS18B20-compatible sensor when its ROM identity disappears
from Linux discovery but the device still responds to conversion and
scratchpad commands.

The normal addressed interface is tried on every poll. If the sensor is absent,
an explicitly enabled fallback uses `SKIP_ROM` through the Linux w1 netlink
connector. This is a real sensor reading; the driver does not invent values or
return a cached temperature. No hardware probe runs during module setup, so a
missing sensor does not itself prevent this module from starting.

## Requirements

- Linux with the w1 subsystem and the w1 netlink connector enabled.
- A **physically dedicated bus containing exactly one sensor**. Software
  discovery cannot establish this when a sensor has lost its identity.
- A DS18B20-compatible sensor with external power (three wires). Parasitic power
  and strong pull-up operation are not implemented or validated.
- The sensor's historical address and the correct Linux w1 master ID.
- Permission to use w1 netlink: the live deployment runs MQTT-IO as root;
  unprivileged operation has not been validated.

Tested with Python 3.9, MQTT-IO 2.2.8, w1thermsensor 2.0.0 and Raspberry Pi
Linux 6.1.21-v8+. Compatibility with other versions has not been established.
The exact chipset of the affected sensor is unknown; this is not a universal
counterfeit-sensor driver.

## Installation

Use the Python interpreter of your MQTT-IO environment. For an existing venv
at `/opt/mqtt-io`, from this repository directory:

```sh
/opt/mqtt-io/bin/python -m pip install 'w1thermsensor>=2.0.0'
SENSOR_DIR=$(/opt/mqtt-io/bin/python -c 'import pathlib, mqtt_io.modules.sensor as s; print(pathlib.Path(s.__file__).parent)')
cp ds18b_resilient.py "$SENSOR_DIR/ds18b_resilient.py"
```

Run the copy with the permissions required by that installation. This adds one
module file without changing MQTT-IO's core or its original `ds18b` module.
Back up your configuration and merge the relevant entries from
[examples/config.yaml](examples/config.yaml). Preserve existing sensor names,
input names and topics if replacing an existing module.

```yaml
sensor_modules:
  - name: inlet
    module: ds18b_resilient
    type: DS18B20
    address: "0123456789ab"  # Historical address, without 28-
    w1_master: 1            # NOT a GPIO number
    single_sensor: true    # Explicit physical wiring assertion
```

Inspect `/sys/bus/w1/devices/w1_bus_master*/w1_master_slaves` and the configured
w1 GPIO buses to establish the mapping. **Master numbering can change** after
reboot or driver reload; verify it before enabling fallback. An empty master
alone is not evidence that it corresponds to your missing sensor.

Restart your MQTT-IO service using its existing service manager. Check logs and
fresh MQTT messages rather than only retained values. On a system using
OverlayFS, install on the persistent filesystem using its maintenance procedure.
Reinstall the module after recreating or upgrading the MQTT-IO environment.

## Reading and failure behaviour

1. Try the normal `w1thermsensor` addressed interface.
2. Fall back only when the expected sysfs sensor is absent and the exception is
   `NoSensorFoundError`. A still-present sensor with a read error does not cause
   broadcast operations.
3. Check the configured bus for unexpected or multiple registered identities.
   Accept `READ_ROM` only if it returns all zeros or the historical identity.
4. Send `RESET + SKIP_ROM + CONVERT_T`, wait one second, then read the scratchpad.
   Each transaction uses the kernel bus mutex; discovery remains enabled.
5. Validate scratchpad length, CRC, configuration bits and temperature range.
   Reject all-zero/all-FF data and the 85 °C power-on value (including a genuine
   measurement of exactly 85 °C). Return to addressed readings when available.

Mode transitions are logged as `address`, `skip_rom` and `unavailable`. Failures
raise an exception to MQTT-IO instead of returning an old value. MQTT-IO or a
subscriber may retain its last published state; this driver does not manage
Home Assistant availability or stale-value timeouts.

The driver honours the reported 9–12 bit resolution and masks undefined bits.
It **does not change resolution or write EEPROM**. A coarse temperature step
can reflect the sensor's current resolution, independently of the raw path.

This workaround does not repair a faulty power supply, cable, ROM or sensor.
A CRC-valid scratchpad after a conversion request cannot independently prove
that a faulty device actually refreshed its internal measurement. Never use
broadcast fallback on a bus with multiple physical sensors, even if discovery
currently lists only one or none.

## Tests

```sh
python3.9 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m unittest discover -s tests -v
```

Eight tests cover startup without probing, addressed/fallback/recovery
transitions, error handling without cached values, bus and ROM identity guards,
conversion commands, CRC checks, power-on rejection and negative temperatures.
Hardware and MQTT operations are mocked; these tests do not operate GPIOs.

The unchanged driver was also tested live over several days with a sensor whose
ROM reads as zero, including two Pi reboots, valid 12-bit scratchpads and fresh
MQTT publications. That evidence concerns one installation, not every clone.

## License

MIT. This is an independent extension, not an official MQTT-IO module.
