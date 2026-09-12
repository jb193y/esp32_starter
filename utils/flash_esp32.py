import os
import glob
import json
import hashlib
import sys
import time
import serial

r"""
Usage:
    # 1. Fast delta-sync (uploads only modified files):
    python utils/flash_esp32.py valve_controller COM21
    python utils/flash_esp32.py hub COM20

    # 2. Complete Chip Erase + MicroPython Flash + Project Upload:
    python utils/flash_esp32.py valve_controller COM4 --erase-flash

    # 3. Clean Filesystem & Upload:
    python utils/flash_esp32.py valve_controller COM21 --clean
"""

def send_command(ser, cmd, timeout=5):
    """Execute raw Python code in Raw REPL and return response."""
    ser.timeout = timeout
    ser.write(cmd.encode('utf-8') + b'\x04')
    response = ser.read_until(b'\x04>')
    if b'Traceback' in response:
        print("Command notice:", response.decode('utf-8', errors='ignore'))
    return response

def enter_raw_repl(ser):
    """Enter raw REPL, supporting both awake boards and deep-sleeping boards."""
    print("Checking ESP32 connection state...")
    ser.timeout = 0.5
    
    # 1. Try immediate hardware reset via DTR/RTS (works if board is awake / powered)
    ser.rts = False
    ser.dtr = False
    ser.setRTS(False)
    ser.setDTR(False)
    time.sleep(0.05)
    ser.setRTS(True)
    time.sleep(0.15)
    ser.setRTS(False)
    time.sleep(0.2)
    ser.reset_input_buffer()
    
    # Send quick interrupt probe
    ser.write(b'\x03\x03')
    time.sleep(0.3)
    quick_resp = ser.read(ser.in_waiting or 100)
    
    # If board responded, try entering raw REPL immediately
    if quick_resp:
        ser.write(b'\x01')
        time.sleep(0.4)
        resp = ser.read(ser.in_waiting or 200)
        if b'raw REPL' in resp:
            print("Connected to Raw REPL (active mode).")
            return
            
    # 2. If board is in Deep Sleep, listen for timer wake-up or EN button press
    print("Device is in Deep Sleep. Waiting for wake-up (press EN button or wait up to 30s)...")
    start_t = time.time()
    caught_wake = False
    
    while time.time() - start_t < 35:
        ser.write(b'\x03')
        if ser.in_waiting:
            chunk = ser.read(ser.in_waiting)
            if b'rst:' in chunk or b'boot' in chunk or b'ESP-ROM' in chunk or b'>>>' in chunk or b'Valve' in chunk:
                print(f" Detected ESP32 wake-up at {time.time()-start_t:.1f}s! Catching boot window...")
                caught_wake = True
                # Send interrupts to break boot.py delay
                for _ in range(6):
                    ser.write(b'\x03\x03')
                    time.sleep(0.1)
                time.sleep(0.3)
                ser.reset_input_buffer()
                break
        time.sleep(0.15)
        
    # 3. Enter raw REPL
    ser.write(b'\x01')
    time.sleep(0.5)
    resp = ser.read(ser.in_waiting or 200)
    
    if b'raw REPL' not in resp:
        ser.write(b'\x03\x03\x01')
        time.sleep(0.5)
        resp += ser.read(ser.in_waiting or 200)
        
    if b'raw REPL' not in resp:
        raise RuntimeError(f"Could not enter raw REPL. Device response: {resp}")
        
    print("Connected to Raw REPL successfully.")

def get_local_file_hash(path):
    """Calculate the SHA256 hash of a local file."""
    h = hashlib.sha256()
    try:
        with open(path, 'rb') as f:
            while True:
                chunk = f.read(4096)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except Exception as e:
        print(f"Error reading local file {path}: {e}")
        return None

def get_device_files_metadata(ser):
    """Query the ESP32 for sizes and SHA256 hashes of all files recursively."""
    device_script = (
        "import os\n"
        "try:\n"
        "    import ubinascii as ubin\n"
        "except:\n"
        "    import binascii as ubin\n"
        "try:\n"
        "    import uhashlib as uhash\n"
        "except:\n"
        "    import hashlib as uhash\n"
        "try:\n"
        "    import ujson as uj\n"
        "except:\n"
        "    import json as uj\n"
        "def hash_file(path):\n"
        "    try:\n"
        "        h = uhash.sha256()\n"
        "        with open(path, 'rb') as f:\n"
        "            while True:\n"
        "                c = f.read(256)\n"
        "                if not c: break\n"
        "                h.update(c)\n"
        "        return ubin.hexlify(h.digest()).decode('utf-8')\n"
        "    except: return None\n"
        "res = {}\n"
        "def walk(path):\n"
        "    try:\n"
        "        for f in os.listdir(path):\n"
        "            p = path + '/' + f if path != '/' else '/' + f\n"
        "            try:\n"
        "                s = os.stat(p)\n"
        "                if s[0] & 0x4000:\n"
        "                    walk(p)\n"
        "                else:\n"
        "                    rel = p.lstrip('/')\n"
        "                    res[rel] = {'size': s[6], 'sha256': hash_file(p)}\n"
        "            except: pass\n"
        "    except: pass\n"
        "walk('/')\n"
        "print('__JSON_START__')\n"
        "print(uj.dumps(res))\n"
        "print('__JSON_END__')\n"
    )
    
    ser.reset_input_buffer()
    time.sleep(0.1)
    resp_bytes = send_command(ser, device_script, timeout=6)
    output = resp_bytes.decode('utf-8', errors='ignore')
    
    if '__JSON_START__' not in output or '__JSON_END__' not in output:
        time.sleep(0.3)
        ser.reset_input_buffer()
        resp_bytes = send_command(ser, device_script, timeout=6)
        output = resp_bytes.decode('utf-8', errors='ignore')

    if '__JSON_START__' not in output or '__JSON_END__' not in output:
        print("Error: Could not parse device metadata response. Raw output:", output)
        return None
        
    json_str = output.split('__JSON_START__')[1].split('__JSON_END__')[0].strip()
    try:
        return json.loads(json_str)
    except Exception as e:
        print(f"Error decoding device files JSON: {e}")
        return None

def upload_file_stream(ser, local_path, remote_path):
    """Stream file content via valid standalone base64 chunks over open raw REPL."""
    import binascii
    with open(local_path, 'rb') as f:
        data = f.read()
        
    # Ensure directory exists if needed
    if '/' in remote_path:
        dir_name = '/'.join(remote_path.split('/')[:-1])
        send_command(ser, f"import os\ntry: os.mkdir('{dir_name}')\nexcept: pass\n")
        
    send_command(ser, f"import ubinascii\nf = open('{remote_path}', 'wb')\n")
    
    # 192 raw bytes -> exactly 256 base64 chars without padding artifacts
    raw_chunk_size = 192
    for i in range(0, len(data), raw_chunk_size):
        raw_chunk = data[i:i+raw_chunk_size]
        b64_chunk = binascii.b2a_base64(raw_chunk).decode('ascii').strip()
        send_command(ser, f"f.write(ubinascii.a2b_base64('{b64_chunk}'))\n")
        
    send_command(ser, "f.close()\n")

def run_erase_flash_and_firmware(port, chip="esp32s3", firmware_path=None, project_root="."):
    import subprocess
    if firmware_path is None:
        firmware_dir = os.path.join(project_root, "firmware")
        pattern = "*S3*.bin" if "s3" in chip.lower() else "ESP32_GENERIC-*.bin"
        matches = sorted(glob.glob(os.path.join(firmware_dir, pattern)), reverse=True)
        if not matches:
            matches = sorted(glob.glob(os.path.join(firmware_dir, "*.bin")), reverse=True)
        if not matches:
            raise RuntimeError(f"No firmware .bin files found in {firmware_dir}")
        firmware_path = matches[0]

    print(f"\n=======================================================")
    print(f" 1. Erasing entire flash on {port} ({chip})...")
    print(f"=======================================================")
    cmd_erase = [sys.executable, "-m", "esptool", "--port", port, "--chip", chip, "erase_flash"]
    print("Executing:", " ".join(cmd_erase))
    res = subprocess.run(cmd_erase)
    if res.returncode != 0:
        print(f"\n[ERROR] esptool erase_flash failed with code {res.returncode}")
        sys.exit(1)

    print(f"\n=======================================================")
    print(f" 2. Writing MicroPython Firmware ({os.path.basename(firmware_path)})...")
    print(f"=======================================================")
    cmd_write = [sys.executable, "-m", "esptool", "--port", port, "--chip", chip, "--baud", "460800", "write_flash", "-z", "0x0", firmware_path]
    print("Executing:", " ".join(cmd_write))
    res = subprocess.run(cmd_write)
    if res.returncode != 0:
        print(f"\n[ERROR] esptool write_flash failed with code {res.returncode}")
        sys.exit(1)

    print("\nFirmware flashed successfully! Waiting for board initialization...")
    time.sleep(2.5)

NODE_PORT_PRESETS = {
    "valve_controller": {
        "COM11": {
            "id": "valve_node_11",
            "custom_name": "COM11",
            "parent_mac": "dc:b4:d9:14:23:3c",  # Direct child of Hub
            "hub_mac": "dc:b4:d9:14:23:3c",
        },
        "COM21": {
            "id": "valve_node_21",
            "custom_name": "COM21",
            "parent_mac": "dc:b4:d9:14:2d:ac",  # Child of COM11
            "hub_mac": "dc:b4:d9:14:23:3c",
        },
        "COM25": {
            "id": "valve_node_25",
            "custom_name": "COM25",
            "parent_mac": "dc:b4:d9:14:2d:50",  # Child of COM21
            "hub_mac": "dc:b4:d9:14:23:3c",
        },
        "COM26": {
            "id": "valve_node_26",
            "custom_name": "COM26",
            "parent_mac": "a0:f2:62:e0:02:d4",  # Child of COM25
            "hub_mac": "dc:b4:d9:14:23:3c",
        },
    },
    "hub": {
        "COM20": {
            "id": "hub_master_01",
            "custom_name": "AgriPulse Master Hub (COM20)",
        },
        "COM24": {
            "id": "hub_master_02",
            "custom_name": "AgriPulse Master Hub (COM24)",
        }
    }
}

def apply_port_config_overrides(target_dir, component_type, port):
    """Dynamically adjust config.json on disk to match COM port configuration preset."""
    config_path = os.path.join(target_dir, "config.json")
    if not os.path.exists(config_path):
        return

    port_upper = port.upper()
    digits = ''.join(c for c in port_upper if c.isdigit()) or "01"
    
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        print(f"Warning: Could not read {config_path} to apply port overrides: {e}")
        return

    presets = NODE_PORT_PRESETS.get(component_type, {}).get(port_upper, {})
    
    if "client" not in cfg or not isinstance(cfg["client"], dict):
        cfg["client"] = {}

    # Override node id and custom name
    if "id" in presets:
        cfg["client"]["id"] = presets["id"]
    elif component_type == "valve_controller":
        cfg["client"]["id"] = f"valve_node_{digits}"
    elif component_type == "hub":
        cfg["client"]["id"] = f"hub_master_{digits}"
    else:
        cfg["client"]["id"] = f"{component_type}_{digits}"

    if "custom_name" in presets:
        cfg["client"]["custom_name"] = presets["custom_name"]
    else:
        cfg["client"]["custom_name"] = port_upper

    # Override parent and hub MACs if applicable
    if "parent_mac" in presets:
        if "parent" not in cfg or not isinstance(cfg["parent"], dict):
            cfg["parent"] = {}
        cfg["parent"]["mac"] = presets["parent_mac"]

    if "hub_mac" in presets:
        if "hub" not in cfg or not isinstance(cfg["hub"], dict):
            cfg["hub"] = {}
        cfg["hub"]["mac"] = presets["hub_mac"]

    try:
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        parent_info = f", parent={cfg.get('parent', {}).get('mac')}" if 'parent' in cfg else ""
        print(f" [Auto-Config] Applied preset for {port_upper}: id={cfg['client']['id']}, custom_name={cfg['client']['custom_name']}{parent_info}")
    except Exception as e:
        print(f"Warning: Failed to save updated config for {port}: {e}")

def main():
    import argparse

    utils_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(utils_dir)
    
    parser = argparse.ArgumentParser(description="Reliable persistent delta-sync & flash tool for ESP32 devices.")
    parser.add_argument("type", help="The project component to deploy (e.g., 'hub', 'valve_controller').")
    parser.add_argument("port", help="The COM port of the ESP32 device (e.g., 'COM21').")
    parser.add_argument("--erase-flash", action="store_true", help="Erase entire flash and flash MicroPython firmware before syncing files.")
    parser.add_argument("--chip", default="esp32s3", help="ESP32 chip type (e.g., 'esp32s3', 'esp32'). Default is 'esp32s3'.")
    parser.add_argument("--firmware", default=None, help="Path to MicroPython firmware .bin file. Defaults to latest in ./firmware/.")
    parser.add_argument("--clean", "--erase-fs", action="store_true", dest="clean_fs", help="Wipe all files on device filesystem before sync.")
    parser.add_argument("--no-auto-config", action="store_true", help="Disable automatic COM port preset injection into config.json.")
    args = parser.parse_args()

    target_dir = os.path.join(project_root, args.type)
    if not os.path.exists(target_dir):
        print(f"Error: Target directory {target_dir} does not exist.")
        sys.exit(1)

    # 1. Apply port-specific config preset unless disabled
    if not args.no_auto_config:
        apply_port_config_overrides(target_dir, args.type, args.port)

    # 2. Run optional full flash erase & firmware write
    if args.erase_flash:
        run_erase_flash_and_firmware(args.port, chip=args.chip, firmware_path=args.firmware, project_root=project_root)

    print(f"\n=======================================================")
    print(f" Connecting to {args.port} for Project File Sync ({args.type})...")
    print(f"=======================================================")
    ser = serial.Serial(args.port, 115200, timeout=2)
    
    try:
        # --- 1. Enter Raw REPL ---
        enter_raw_repl(ser)
        
        # --- 2. Query Device Files for Delta Sync ---
        print(f"Scanning filesystem on {args.port}...")
        device_files = get_device_files_metadata(ser)
        if device_files is None:
            print("[ERROR] Failed to query device metadata.")
            sys.exit(1)
            
        # Compile local expected files
        expected_files = {}
        
        # Project configs
        for f in glob.glob(os.path.join(target_dir, 'config*.json')):
            rel = os.path.basename(f)
            expected_files[rel] = f
            
        # Project python files
        for f in glob.glob(os.path.join(target_dir, '*.py')):
            basename = os.path.basename(f)
            if basename not in ('flash_esp32.py', 'verify_device.py', 'pack_code.py', 'unpack_code.py'):
                expected_files[basename] = f
                
        # Shared lib python files
        for f in glob.glob(os.path.join(project_root, 'lib', '*.py')):
            rel = f"lib/{os.path.basename(f)}"
            expected_files[rel] = f
            
        # --- 3. Clean up unwanted files ---
        preserve_list = set() if args.clean_fs else {'events.jsonl', 'faults.jsonl', 'config.json'}
        unwanted_files = []
        for dev_file in device_files.keys():
            if dev_file.endswith('.bak'):
                continue
            if args.clean_fs or (dev_file not in expected_files and dev_file not in preserve_list):
                unwanted_files.append(dev_file)
                
        if unwanted_files:
            print(f"Cleanup: Deleting {len(unwanted_files)} {'all' if args.clean_fs else 'obsolete'} files from device...")
            for f in unwanted_files:
                print(f" - Removing {f}...")
                send_command(ser, f"import os\ntry: os.remove('{f}')\nexcept: pass\n")
            if args.clean_fs:
                device_files = {}
        else:
            print("Filesystem clean (no obsolete files).")
            
        # --- 4. Ensure :lib directory exists ---
        send_command(ser, "import os\ntry: os.mkdir('lib')\nexcept: pass\n")
        
        # --- 5. Delta Sync Files ---
        print("\nChecking delta file sync status...")
        up_to_date_files = []
        out_of_sync_files = []
        
        for rel_path, local_abs_path in sorted(expected_files.items()):
            local_size = os.path.getsize(local_abs_path)
            local_hash = get_local_file_hash(local_abs_path)
            
            is_synced = False
            if rel_path in device_files:
                dev_meta = device_files[rel_path]
                if dev_meta.get('size') == local_size and dev_meta.get('sha256') == local_hash:
                    is_synced = True
                    
            if is_synced:
                up_to_date_files.append(rel_path)
            else:
                out_of_sync_files.append((rel_path, local_abs_path))
                
        if up_to_date_files:
            print("\nUp-to-date files (skipped):")
            for rel_path in up_to_date_files:
                print(f" [=] {rel_path}")
                
        if out_of_sync_files:
            print(f"\nFlashing {len(out_of_sync_files)} updated/missing files...")
            for rel_path, local_abs_path in out_of_sync_files:
                print(f" [^] Uploading {rel_path}...")
                upload_file_stream(ser, local_abs_path, rel_path)
            print("All updated files uploaded successfully.")
        else:
            print("\nAll files are already up-to-date! No transfer needed.")
            
        # --- 6. Reset Device ---
        print("\nSync completed. Resetting device...")
        ser.write(b'\x02\x04') # Ctrl-B (exit raw REPL) + Ctrl-D (soft reboot)
        time.sleep(0.5)
        print("Done!")
        
    finally:
        ser.close()

if __name__ == '__main__':
    main()
