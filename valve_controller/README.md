# AgriPulse Valve Controller (VC)

Firmware and configuration for AgriPulse 8-channel / 16-channel latching solenoid valve controllers (ESP32-S3).

---

## 1. Fast Deployment via `utils`

From `esp32_projects`:

```powershell
# Flash & sync all code + libraries to COM25
python utils/flash_esp32.py valve_controller COM25
```

---

## 2. Configuration (`config.json`)

Key configuration sections:
- `client.id`: Unique device identifier (e.g. `valve_node_25`).
- `client.deep_sleep_enabled`: `true` or `false`.
- `hub.mac`: Destination Master Hub MAC address (e.g. `dc:b4:d9:14:23:3c`).
- `parent.mac`: Next-hop MAC address (`dc:b4:d9:14:23:3c` for direct or `dc:b4:d9:14:2d:ac` to relay via COM11).
- `valves`: Pin mapping for open/close solenoid H-bridge triggers and status LEDs.

---

## 3. Verification & Live Monitoring

```powershell
# Verify files & SHA-256 checksums
python utils/verify_device.py COM25

# Live REPL log monitoring
mpremote connect COM25 repl
```
