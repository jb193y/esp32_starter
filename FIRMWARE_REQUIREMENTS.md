# AgriPulse Firmware Specification & Requirements

This document specifies the required MicroPython version, built-in C extensions, and hardware requirements for AgriPulse Master Hub and Sub-Node controllers (Pump Controllers, Valve Controllers).

---

## 1. Required MicroPython Firmware Build

> [!IMPORTANT]
> All ESP32 and ESP32-S3 hardware nodes **MUST** run MicroPython version **`>= v1.21.0`** (Recommended: **`v1.27.0`**).
> Older builds (such as `v1.19.x`) do not include built-in `espnow` C extensions and will fail to boot ESP-NOW mesh networking.

### Mandatory Built-in C Modules:
1. **`espnow`**: Native C extension for peer-to-peer hardware mesh communication, relay routing, and ACK telemetry.
2. **`bluetooth` (`ubluetooth`)**: BLE GATT server used for zero-touch mobile app Wi-Fi & MQTT provisioning.
3. **`neopixel` & `machine`**: GPIO Pin control, UART/I2C interfaces, and WS2812 RGB LED status indicator support.
4. **`_thread`**: Hardware multithreading for watchdog, LED animations, network managers, and MQTT client.

---

## 2. Firmware Binary References

Pre-compiled official firmware binaries with `espnow` and `bluetooth` enabled are stored in:
`E:\00.0. Jayanti Baraiya - NSAShared\04.A ESP32\firmware\`

- **ESP32-S3 Nodes**: `ESP32_GENERIC_S3-20251209-v1.27.0.bin`
- **Standard ESP32 Nodes**: `ESP32_GENERIC-20251209-v1.27.0.bin`

---

## 3. Flashing Instructions

To flash/upgrade an ESP32 node to MicroPython `v1.27.0`:

1. **Enter ROM Bootloader Mode**:
   - Hold the **BOOT** button on the ESP32 board.
   - Press and release the **RESET** (EN) button.
   - Release the **BOOT** button.

2. **Erase Flash**:
   ```bash
   python -m esptool --port COM12 --chip esp32s3 erase-flash
   ```

3. **Flash MicroPython v1.27.0 Binary**:
   ```bash
   python -m esptool --port COM12 --chip esp32s3 write-flash 0 "..\firmware\ESP32_GENERIC_S3-20251209-v1.27.0.bin"
   ```

4. **Upload Firmware Code**:
   ```bash
    mpremote connect port:COM12 cp valve_controller/boot.py :boot.py + cp valve_controller/config.defaults.json :config.defaults.json + cp valve_controller/config.json :config.json + cp valve_controller/main.py :main.py + cp lib/config.py :lib/config.py + cp lib/led_status.py :lib/led_status.py + cp lib/ble_manager.py :lib/ble_manager.py + cp lib/espnow_client.py :lib/espnow_client.py + cp lib/espnow_relay.py :lib/espnow_relay.py + cp lib/factory_reset.py :lib/factory_reset.py
   ```
