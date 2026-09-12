# AgriPulse ESP32 Firmware & Node Tooling

This repository contains the MicroPython firmware, shared mesh libraries, configuration templates, and deployment utilities for AgriPulse ESP32 / ESP32-S3 controllers:
- **Master Hub Controller** (`/hub`)
- **Valve Controller (VC)** (`/valve_controller`)
- **Pump Controller** (`/pump_controller`)
- **Shared Libraries & Mesh Protocols** (`/lib`)
- **Deployment & Diagnostic Tooling** (`/utils`)

---

## 1. Setup & Environment

Open PowerShell in the `esp32_projects` directory and activate the virtual environment:

```powershell
cd "e:\00.0. Jayanti Baraiya - NSAShared\04.A ESP32\esp32_projects"
.\.venv\Scripts\Activate.ps1
```

*(All required tools including `esptool.py`, `pyserial`, `mpremote`, and `paho-mqtt` are pre-installed in `.venv`)*.

---

## 2. Quick Deploy with `utils/flash_esp32.py` (Recommended)

`utils/flash_esp32.py` is the official automated deployment utility. It provides:
- **Deep Sleep Auto-Wake**: Automatically catches the boot interrupt window if the node is sleeping.
- **SHA-256 Delta Sync**: Only flashes modified files, preserving on-device logs and saving flash wear.
- **Auto-Bundles `/lib`**: Automatically syncs all required shared libraries (`espnow_client.py`, `espnow_relay.py`, `config.py`, etc.).
- **Automatic Soft Reset**: Reboots the board cleanly after deployment.

### Commands:

#### Flash / Deploy Valve Controller (e.g., COM25 or COM11):
```powershell
python utils/flash_esp32.py valve_controller COM25
```

#### Flash / Deploy Master Hub (e.g., COM20):
```powershell
python utils/flash_esp32.py hub COM20
```

#### Flash / Deploy Pump Controller (e.g., COM24):
```powershell
python utils/flash_esp32.py pump_controller COM24
```

---

## 3. Verifying On-Device Files

To verify that all files and SHA256 checksums match between your workstation and the physical ESP32 node:

```powershell
python utils/verify_device.py COM25
```

---

## 4. Multi-Port Serial Monitoring

To monitor real-time ESP-NOW mesh envelopes, relays, and MQTT dispatches simultaneously across Hub and Nodes:

```powershell
python utils/dual_serial_monitor.py
```

*Or monitor a single port directly via mpremote:*
```powershell
mpremote connect COM25 repl
```
*(Press `Ctrl+]` to exit mpremote REPL).*

---

## 5. First-Time MicroPython Firmware Flashing (Fresh Boards)

If flashing a brand new ESP32 / ESP32-S3 module that does not yet have MicroPython `>= v1.21.0`:

```powershell
# 1. Erase Flash
python -m esptool --port COM4 --chip esp32s3 erase-flash
OR
esptool.py --chip esp32s3 --port COM4 erase-flash

# 2. Flash MicroPython Binary
python -m esptool --port COM4 --chip esp32s3 write-flash -z 0x0 ./firmware/ESP32_GENERIC_S3-20260824-v1.29.0.bin
OR
esptool.py --chip esp32s3 --port COM4 --baud 460800 write-flash -z 0x0 ./firmware/ESP32_GENERIC_S3-20260824-v1.29.0.bin

# 3. Deploy Application Code & Config
python utils/flash_esp32.py valve_controller COM4
```

---

## 6. Valve Controller Mesh Topology Configuration

In `valve_controller/config.json`:

### Direct Hop to Hub (Level 1 Node):
```json
{
  "client": {
    "id": "valve_node_11",
    "custom_name": "COM11",
    "deep_sleep_enabled": false
  },
  "wifi": { "channel": 11 },
  "hub": { "mac": "dc:b4:d9:14:23:3c" },
  "parent": { "mac": "dc:b4:d9:14:23:3c" }
}
```

### Relayed Hop via COM11 (Level 2 Leaf Node - e.g., COM25):
```json
{
  "client": {
    "id": "valve_node_25",
    "custom_name": "COM25",
    "deep_sleep_enabled": false
  },
  "wifi": { "channel": 11 },
  "hub": { "mac": "dc:b4:d9:14:23:3c" },
  "parent": { "mac": "dc:b4:d9:14:2d:ac" }
}
```

---

## 7. Manual Fallback via `mpremote`

If manual file copying is ever needed:

```powershell
mpremote connect COM25 fs mkdir /lib
mpremote connect COM25 fs cp lib/config.py :lib/config.py
mpremote connect COM25 fs cp lib/espnow_client.py :lib/espnow_client.py
mpremote connect COM25 fs cp lib/espnow_relay.py :lib/espnow_relay.py
mpremote connect COM25 fs cp lib/espnow_ota.py :lib/espnow_ota.py
mpremote connect COM25 fs cp lib/message_builder.py :lib/message_builder.py
mpremote connect COM25 fs cp lib/led_status.py :lib/led_status.py
mpremote connect COM25 fs cp lib/ble_manager.py :lib/ble_manager.py
mpremote connect COM25 fs cp lib/ota.py :lib/ota.py
mpremote connect COM25 fs cp lib/factory_reset.py :lib/factory_reset.py
mpremote connect COM25 fs cp valve_controller/boot.py :boot.py
mpremote connect COM25 fs cp valve_controller/config.json :config.json
mpremote connect COM25 fs cp valve_controller/main.py :main.py
mpremote connect COM25 soft-reset
```
