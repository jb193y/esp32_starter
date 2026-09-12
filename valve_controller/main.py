# main.py (Valve Controller)
import network
import _thread
import time
import random
import machine
import os
import gc
import config
import led_status
import ble_manager
import espnow_client
import factory_reset
import espnow_ota

# State
valves = {}
last_telemetry_time = 0
next_telemetry_delay = 30

# Initialize OTA Receiver for ESP-NOW firmware updates
ota_receiver = espnow_ota.OTAReceiver(espnow_client.send_ack_or_tele_to_hub)

# Non-blocking command queue — receive thread enqueues, main loop executes
_cmd_queue = []
_provision_confirmed = False

def save_valve_states():
    """Saves valve states to both RTC memory (for zero-wear deep sleep) and flash file."""
    try:
        import ujson
        states = {vid: v["state"] for vid, v in valves.items()}
        
        # 1. RTC Slow Memory (survives deep sleep, 0 flash wear)
        try:
            rtc = machine.RTC()
            rtc.memory(ujson.dumps(states))
        except Exception as rtc_err:
            pass
            
        # 2. Flash file (for cold boot / complete power loss recovery)
        with open("valve_states.json", "w") as f:
            ujson.dump(states, f)
    except Exception as e:
        print(" Failed to save valve states:", e)

def load_valve_states():
    """Loads valve states prioritizing RTC memory, falling back to flash."""
    # 1. Check RTC Slow Memory first
    try:
        import ujson
        rtc = machine.RTC()
        data = rtc.memory()
        if data:
            states = ujson.loads(data)
            if isinstance(states, dict) and states:
                print(" Restored valve states from RTC memory:", states)
                return states
    except Exception:
        pass

    # 2. Fallback to flash file
    try:
        import ujson
        import os
        if "valve_states.json" in os.listdir():
            with open("valve_states.json", "r") as f:
                states = ujson.load(f)
                print(" Restored valve states from flash storage:", states)
                return states
    except Exception as e:
        print(" Failed to load valve states:", e)
    return {}

def update_valve_leds(valve_id):
    valve = valves.get(valve_id)
    if not valve:
        return
    state = valve.get("state", "CLOSED")
    
    status_led = valve.get("status_led_pin")
    
    if status_led:
        if state == "OPEN":
            status_led.value(1)
        elif state == "FAULT":
            status_led.value(1)
        else: # CLOSED
            status_led.value(0)

    # Update main system status LED based on overall node state
    any_fault = any(v.get("state") == "FAULT" for v in valves.values())
    any_open = any(v.get("state") == "OPEN" for v in valves.values())
    
    if any_fault:
        led_status.set_status("FAULT")
    elif any_open:
        led_status.set_status("VALVE_OPEN")
    else:
        led_status.set_status("VALVE_CLOSED")

def pulse_solenoid(valve_id="1", open_pulse=True):
    valve = valves.get(valve_id)
    if not valve:
        print(f" Valve {valve_id} not found!")
        return
        
    open_pin = valve.get("solenoid_open_pin")
    close_pin = valve.get("solenoid_close_pin")
    if open_pin is None or close_pin is None:
        print(f" Valve {valve_id} pins not configured!")
        return
        
    print(f" Solenoid Valve {valve_id} Action: {'OPENING' if open_pulse else 'CLOSING'} pulse starting...")
    
    open_pin.value(0)
    close_pin.value(0)
    time.sleep_ms(10)
    
    if open_pulse:
        open_pin.value(1)
        time.sleep_ms(100) # 100ms pulse
        open_pin.value(0)
        valve["state"] = "OPEN"
    else:
        close_pin.value(1)
        time.sleep_ms(100) # 100ms pulse
        close_pin.value(0)
        valve["state"] = "CLOSED"
        
    update_valve_leds(valve_id)
    save_valve_states()
    print(f" Solenoid Valve {valve_id} Action complete. State: {valve['state']}")

def handle_hub_commands(cmd_or_packet, args_or_sender=None):
    if isinstance(cmd_or_packet, dict):
        msg_type = cmd_or_packet.get("type") or cmd_or_packet.get("msg_type") or cmd_or_packet.get("t")
        data = cmd_or_packet.get("action") or cmd_or_packet.get("config") or cmd_or_packet.get("data") or cmd_or_packet.get("pld") or {}
        cmd = data.get("cmd") or data.get("command")
        if not cmd and isinstance(data, dict):
            if "set_valves" in data:
                cmd = "SET_VALVES"
            elif "valve_id" in data and ("command" in data or "state" in data):
                cmd = data.get("command") or ("VALVE_OPEN" if data.get("state") == "OPEN" else "VALVE_CLOSE")
        sender_mac = args_or_sender or cmd_or_packet.get("source") or cmd_or_packet.get("src")
    else:
        cmd = cmd_or_packet
        data = args_or_sender if isinstance(args_or_sender, dict) else {}
        sender_mac = data.get("sender_mac")
        msg_type = "COMMAND" if cmd else "ACK"

    print(f" Received from Hub ({msg_type}): cmd={cmd}, data={data}")

    # Process ESP-NOW OTA packets directly for minimum latency
    if isinstance(data, dict) and (cmd == "OTA" or str(data.get("action", "")).startswith("OTA_")):
        led_status.set_status("BLE_PROVISIONING")
        ota_receiver.handle_packet(data, sender_mac)
        return

    if msg_type in ("CMD", "COMMAND"):
        _cmd_queue.append((cmd, data, sender_mac))
    elif msg_type == "ACK":
        status_val = data.get("status") if isinstance(data, dict) else None
        cfg = config.load_config()
        if status_val == "sleep_ok":
            if cfg.get("client", {}).get("deep_sleep_enabled", True):
                print(" Hub returned SLEEP_OK.")
            else:
                print(" Hub ACK: Telemetry received (Continuous Mode).")
        else:
            print(" Hub ACK:", status_val)

def execute_command(cmd, args, sender_mac=None):
    if not cmd:
        return
        
    print(f" Executing Hub command: {cmd} with args: {args}")
    
    if cmd == "SET_VALVES":
        # Target states can be passed as {"set_valves": {...}} or {"valves": {...}} or directly as {...}
        target_states = (args.get("set_valves") or args.get("valves")) if isinstance(args, dict) else args
        
        if isinstance(target_states, dict):
            # Check for global ALL or '0' shortcut (e.g. {"ALL": "CLOSED"} or {"0": "OPEN"})
            global_action = target_states.get("ALL") or target_states.get("all") or target_states.get("0")
            if global_action:
                open_pulse = (str(global_action).upper() == "OPEN")
                for vid in valves.keys():
                    pulse_solenoid(vid, open_pulse=open_pulse)
            else:
                for vid, target_state in target_states.items():
                    if str(vid) in valves:
                        pulse_solenoid(str(vid), open_pulse=(str(target_state).upper() in ("OPEN", "ON", "1", "TRUE")))
        
        any_open = any(v.get("state") == "OPEN" for v in valves.values())
        espnow_client.send_ack_or_tele_to_hub("ACK", {
            "status": "VALVES_UPDATED",
            "cmd": "SET_VALVES",
            "valves": {vid: v["state"] for vid, v in valves.items()},
            "node_status": "watering" if any_open else "valve_idle"
        }, target_mac=sender_mac)

    elif cmd in ("VALVE_OPEN", "VALVE_CLOSE"):
        valve_id = str(args.get("valve_id", 1)) if isinstance(args, dict) else "1"
        open_pulse = (cmd == "VALVE_OPEN")
        if valve_id in valves:
            pulse_solenoid(valve_id, open_pulse=open_pulse)
        any_open = any(v.get("state") == "OPEN" for v in valves.values())
        espnow_client.send_ack_or_tele_to_hub("ACK", {
            "status": "VALVES_UPDATED",
            "cmd": cmd,
            "valves": {vid: v["state"] for vid, v in valves.items()},
            "node_status": "watering" if any_open else "valve_idle"
        }, target_mac=sender_mac)

    elif cmd == "COM_TEST":
        print(" Visual COM_TEST triggered on Valve Controller!")
        try:
            prev_status = getattr(led_status, "_state", "VALVE_CLOSED")
            led_status.set_status("BLE_PROVISIONING")
            time.sleep(1.5)
            led_status.set_status(prev_status)
            any_open = any(v.get("state") == "OPEN" for v in valves.values())
            espnow_client.send_ack_or_tele_to_hub("ACK", {
                "status": "COM_TEST_OK",
                "cmd": "COM_TEST",
                "valves": {vid: v["state"] for vid, v in valves.items()},
                "node_status": "watering" if any_open else "valve_idle"
            }, target_mac=sender_mac)
        except Exception as e:
            print("COM_TEST error:", e)

    elif cmd == "GET_STATUS":
        any_open = any(v.get("state") == "OPEN" for v in valves.values())
        espnow_client.send_ack_or_tele_to_hub("ACK", {
            "valves": {vid: v["state"] for vid, v in valves.items()},
            "node_status": "watering" if any_open else "valve_idle"
        }, target_mac=sender_mac)

    elif cmd == "SET_CONFIG":
        print(" SET_CONFIG command received:", args)
        try:
            config_payload = args.get("config") or args.get("settings") or args
            if isinstance(config_payload, dict):
                clean_payload = {k: v for k, v in config_payload.items() if k not in ("cmd", "command", "target", "source", "msg_type")}
                config.update_config(clean_payload)
                print(" Configuration updated successfully on Valve Controller.")
            
            any_open = any(v.get("state") == "OPEN" for v in valves.values())
            espnow_client.send_ack_or_tele_to_hub("ACK", {
                "status": "CONFIG_UPDATED",
                "cmd": "SET_CONFIG",
                "valves": {vid: v["state"] for vid, v in valves.items()},
                "node_status": "watering" if any_open else "valve_idle"
            }, target_mac=sender_mac)
            
            if isinstance(args, dict) and args.get("reboot"):
                time.sleep_ms(300)
                machine.reset()
        except Exception as cfg_err:
            print(" SET_CONFIG failed:", cfg_err)

    elif cmd == "REBOOT":
        print(" REBOOT command received! Rebooting Valve Controller...")
        try:
            any_open = any(v.get("state") == "OPEN" for v in valves.values())
            espnow_client.send_ack_or_tele_to_hub("ACK", {
                "status": "REBOOTING",
                "cmd": "REBOOT",
                "valves": {vid: v["state"] for vid, v in valves.items()},
                "node_status": "watering" if any_open else "valve_idle"
            }, target_mac=sender_mac)
        except Exception:
            pass
        time.sleep_ms(300)
        machine.reset()

    elif cmd in ("CONFIRM_PROVISION", "CLAIM_CONFIRM"):
        global _provision_confirmed
        print(" Provisioning Claim Confirmed by Backend!")
        _provision_confirmed = True
        try:
            cfg = config.load_config()
            cfg.setdefault("client", {})["mode"] = "normal"
            config.save_config(cfg)
        except Exception as ex:
            print("Error updating mode to normal:", ex)

        any_open = any(v.get("state") == "OPEN" for v in valves.values())
        node_id = cfg.get("client", {}).get("id", "valve_node")
        espnow_client.send_ack_or_tele_to_hub("ACK", {
            "status": "provisioned",
            "device_id": node_id,
            "node_status": "online",
            "valves": {vid: v["state"] for vid, v in valves.items()}
        }, target_mac=sender_mac)
        
        led_status.set_status("VALVE_CLOSED")

    elif cmd == "OTA":
        print(" OTA command received:", args)
        if isinstance(args, dict) and "action" in args:
            led_status.set_status("BLE_PROVISIONING")
            ota_receiver.handle_packet(args, sender_mac)
        else:
            # Fallback direct Wi-Fi OTA if full URL provided and Wi-Fi credentials exist
            try:
                import network_manager
                cfg = config.load_config()
                wifi_networks = cfg.get("wifi", {}).get("networks", [])
                if wifi_networks and network_manager.connect():
                    import ota
                    base_url = args.get("url") if isinstance(args, dict) else None
                    if not base_url:
                        base_url = cfg.get("ota", {}).get("base_url", "http://10.10.10.211:8000/fw")
                    manifest = ota.fetch_manifest(base_url)
                    if ota.ota_update(base_url, manifest=manifest):
                        time.sleep(1)
                        machine.reset()
                else:
                    espnow_client.send_ack_or_tele_to_hub("ACK", {
                        "status": "OTA_READY_FOR_ESPNOW",
                        "valves": {vid: v["state"] for vid, v in valves.items()},
                        "node_status": "active"
                    }, target_mac=sender_mac)
            except Exception as ota_err:
                print(" Direct Wi-Fi OTA failed:", ota_err)

def main():
    global last_telemetry_time, next_telemetry_delay
    print(" Valve Controller Starting...")
    
    # 1. Start Status LED
    _thread.start_new_thread(led_status.led_thread, ())
    time.sleep(0.2)
    factory_reset.start()
    
    # 2. Load configurations
    cfg_exists = "config.json" in os.listdir()
    cfg = None
    if cfg_exists:
        try:
            cfg = config.load_config()
        except Exception:
            cfg = None
            
    if cfg is None:
        print(" Configuration missing or corrupt! Falling back to BLE Provisioning Mode.")
        led_status.set_status("BLE_PROVISIONING")
        ble_manager.start_provisioning()
        return
        
    client_cfg = cfg.get("client", {})
    mode = client_cfg.get("mode", "ble_setup")
    
    if mode == "ble_setup":
        print(" BLE Setup mode configured. Initializing provisioning...")
        led_status.set_status("BLE_PROVISIONING")
        ble_manager.start_provisioning()
        return

    # 3. Start Normal Operations
    print(" Loading Valve Outputs...")
    led_status.set_status("VALVE_CLOSED")
    
    valves_cfg = cfg.get("valves", [])
    valves_map = {}
    if isinstance(valves_cfg, list) and len(valves_cfg) > 0:
        valves_map = valves_cfg[0]
    elif isinstance(valves_cfg, dict):
        valves_map = valves_cfg

    # Load previously saved states from RTC memory or flash
    saved_states = load_valve_states()

    # Initialize all valves
    for vid, pins in valves_map.items():
        open_pin_num = pins.get("solenoid_open")
        close_pin_num = pins.get("solenoid_close")
        status_led_num = pins.get("status_led")
        
        # Solenoids
        open_pin = machine.Pin(open_pin_num, machine.Pin.OUT) if open_pin_num is not None else None
        close_pin = machine.Pin(close_pin_num, machine.Pin.OUT) if close_pin_num is not None else None
        
        # LED
        status_led_pin = machine.Pin(status_led_num, machine.Pin.OUT) if status_led_num is not None else None
        
        if open_pin: open_pin.value(0)
        if close_pin: close_pin.value(0)
        
        valves[str(vid)] = {
            "state": saved_states.get(str(vid), "CLOSED"),
            "solenoid_open_pin": open_pin,
            "solenoid_close_pin": close_pin,
            "status_led_pin": status_led_pin
        }
        
        # Initialize LEDs status
        update_valve_leds(str(vid))
    
    # Initialize ESP-NOW client
    espnow_client.init_espnow_client()
    heartbeats = {"esp_now": time.time()}
    _thread.start_new_thread(espnow_client.client_tx_loop, ())
    _thread.start_new_thread(espnow_client.client_listen_loop, (heartbeats, handle_hub_commands))

    # 4. Handle "pending_confirm" Provisioning Claim Workflow (Same as Hub):
    if mode == "pending_confirm":
        print(" Device in BLE pending_confirm mode. Starting Hub pairing & claim confirmation...")
        led_status.set_status("BLE_PROVISIONING")

        # 4.1 Step 1: Pair with Hub via ESP-NOW
        pair_start = time.time()
        while not espnow_client.is_paired() and (time.time() - pair_start < 45):
            if _cmd_queue:
                cmd, args, sender_mac = _cmd_queue.pop(0)
                execute_command(cmd, args, sender_mac)
            time.sleep_ms(100)

        if not espnow_client.is_paired():
            print(" Hub pairing timed out (45s). Reverting to BLE setup mode...")
            cfg.setdefault("client", {})["mode"] = "ble_setup"
            config.save_config(cfg)
            time.sleep(1)
            machine.reset()
            return

        print(" Connected to Hub via ESP-NOW! Notifying Hub & Backend that Claim is Pending...")

        # 4.2 Step 2: Notify Hub & Backend that Claim is Pending
        node_id = client_cfg.get("id", "valve_node")
        espnow_client.send_ack_or_tele_to_hub("PROVISIONING", {
            "step": "CLAIM_PENDING",
            "status": "BLE_CLAIM_PENDING",
            "node_id": node_id,
            "node_type": "VALVE",
            "custom_name": client_cfg.get("custom_name", node_id),
            "site": client_cfg.get("site", "default_site")
        })

        # 4.3 Step 3: Wait up to 90s for CONFIRM_PROVISION from Backend
        claim_start = time.time()
        print(" Device in claim wait mode. Waiting up to 90s for confirmation from backend...")
        while not _provision_confirmed and (time.time() - claim_start < 90):
            if _cmd_queue:
                cmd, args, sender_mac = _cmd_queue.pop(0)
                execute_command(cmd, args, sender_mac)
                if _provision_confirmed:
                    break
            time.sleep_ms(100)

        if not _provision_confirmed:
            print(" Claim confirmation window (90s) elapsed! Reverting to BLE setup mode...")
            cfg.setdefault("client", {})["mode"] = "ble_setup"
            config.save_config(cfg)
            time.sleep(1)
            machine.reset()
            return

        print(" Provisioning & Claiming Complete! Mode updated to 'normal'.")
        led_status.set_status("VALVE_CLOSED")

    # 5. Normal Operations
    deep_sleep_enabled = client_cfg.get("deep_sleep_enabled", True)
    deep_sleep_sec = int(client_cfg.get("deep_sleep_sec", 30))

    if deep_sleep_enabled:
        print(f" [Power Mode] Deep Sleep Configured (Base interval: {deep_sleep_sec}s)")

        # Determine Mesh Tier (Tier 1 = Direct/Relay, Tier 2 = Leaf multi-hop)
        hub_mac = cfg.get("hub", {}).get("mac", "")
        parent_mac = cfg.get("parent", {}).get("mac", "")
        is_leaf = bool(parent_mac and parent_mac != hub_mac and parent_mac != "00:00:00:00:00:00")
        mesh_tier = 2 if is_leaf else 1

        # 1. Micro-Jitter / Pseudo-TDMA slot delay (eliminates collisions)
        sta = network.WLAN(network.STA_IF)
        local_mac = espnow_client.bytes_to_mac(sta.config('mac'))
        slot_delay_ms = espnow_client.calculate_slot_jitter_ms(local_mac, tier=mesh_tier)
        print(f" [Micro-Jitter] Tier {mesh_tier} slot delay: {slot_delay_ms}ms")
        time.sleep_ms(slot_delay_ms)

        # 2. Send Check-In / Telemetry to Hub
        any_open = any(v.get("state") == "OPEN" for v in valves.values())
        node_status = "watering" if any_open else "valve_idle"
        telemetry = {
            "status": node_status,
            "valves": {vid: v["state"] for vid, v in valves.items()},
            "rssi": -50,
            "sleep_sec": deep_sleep_sec
        }
        espnow_client.send_ack_or_tele_to_hub("TELE", telemetry)
        print(" Check-In Telemetry sent to Hub. Listening for commands...")

        # 3. Wait up to 3500ms for incoming Hub response / mailbox commands
        start_wait = time.ticks_ms()
        cmd_executed = False
        while time.ticks_diff(time.ticks_ms(), start_wait) < 3500:
            if _cmd_queue:
                cmd, args, sender_mac = _cmd_queue.pop(0)
                execute_command(cmd, args, sender_mac)
                cmd_executed = True
                start_wait = time.ticks_ms()
            time.sleep_ms(30)
            if cmd_executed and not _cmd_queue:
                time.sleep_ms(350)
                break

        # 4. If an OTA update session is in progress, Discovery Mode is active, or Relay traffic is in-flight, stay awake!
        while ota_receiver.is_in_progress() or not espnow_client.can_deep_sleep():
            if _cmd_queue:
                cmd, args, sender_mac = _cmd_queue.pop(0)
                execute_command(cmd, args, sender_mac)
            time.sleep_ms(50)

        # 5. Wait for TX queue to finish transmitting any pending ACK frames
        tx_wait = time.ticks_ms()
        while not espnow_client.tx_queue.empty() and time.ticks_diff(time.ticks_ms(), tx_wait) < 1500:
            time.sleep_ms(20)

        # 6. Enter Closed-Loop Synced Deep Sleep
        sleep_duration_ms = espnow_client.get_next_wake_delay_ms(default_sec=deep_sleep_sec)
        print(f" Going to Deep Sleep for {sleep_duration_ms}ms ({sleep_duration_ms/1000:.1f}s). Goodnight!")
        time.sleep_ms(50)
        try:
            espnow_client.stop_client()
        except:
            pass
        machine.deepsleep(sleep_duration_ms)

    else:
        # Continuous Running Loop (Non-sleep mode)
        print(" [Power Mode] Continuous Loop Active (Deep sleep disabled).")
        
        # Determine Mesh Tier (Tier 1 = Direct/Relay, Tier 2 = Leaf multi-hop)
        hub_mac = cfg.get("hub", {}).get("mac", "")
        parent_mac = cfg.get("parent", {}).get("mac", "")
        is_leaf = bool(parent_mac and parent_mac != hub_mac and parent_mac != "00:00:00:00:00:00")
        mesh_tier = 2 if is_leaf else 1
        tier_offset = 0 if mesh_tier >= 2 else 2.5

        while True:
            try:
                gc.collect()

                # Drain command queue
                if _cmd_queue:
                    cmd, args, sender_mac = _cmd_queue.pop(0)
                    execute_command(cmd, args, sender_mac)

                # Send periodic telemetry (staggered by tier to prevent RF collisions)
                if espnow_client.is_paired():
                    now = time.time()
                    if now - last_telemetry_time >= (next_telemetry_delay + tier_offset):
                        last_telemetry_time = now
                        any_open = any(v.get("state") == "OPEN" for v in valves.values())
                        node_status = "watering" if any_open else "valve_idle"
                        telemetry = {
                            "status": node_status,
                            "valves": {vid: v["state"] for vid, v in valves.items()},
                            "rssi": -50
                        }
                        espnow_client.send_ack_or_tele_to_hub("TELE", telemetry)

                time.sleep_ms(100)

            except Exception as err:
                print(" Valve Loop Error:", err)
                time.sleep(1)
            
try:
    main()
except KeyboardInterrupt:
    print(" Keyboard interrupt received; stopping Valve Controller...")
    try:
        espnow_client.stop_client()
    except Exception as stop_err:
        pass
    print(" Valve Controller stopped")
except Exception as e:
    print(" Main loop error:", e)
