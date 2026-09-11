# espnow_client.py (Shared ESP-NOW Client Library)
import network
try:
    import espnow
    has_espnow = True
except ImportError:
    espnow = None
    has_espnow = False

import ujson
import time
import config
import espnow_relay
import message_builder

def set_wifi_channel(ch):
    ap = network.WLAN(network.AP_IF)
    ap.active(True)
    try:
        ap.config(channel=ch)
    except Exception:
        pass
    sta = network.WLAN(network.STA_IF)
    sta.active(True)
    try:
        sta.config(channel=ch)
    except Exception:
        pass

_e = None
_paired = False
_last_hub_rx_time = time.time()
_last_pairing_tx_time = 0
_stop_requested = False
_next_wake_delay_ms = 0
_discovery_awake_until = 0
_relay_busy_until = 0
_last_beacon_broadcast_time = 0
_candidate_beacons = {}
_discovery_lock_channel = None
_candidate_settle_until = 0
MAX_RX_BUFFER = 2048

tx_queue = config.Queue()

def touch_relay_activity(duration_sec=30):
    global _relay_busy_until
    _relay_busy_until = max(_relay_busy_until, int(time.time()) + duration_sec)

def can_deep_sleep():
    global _discovery_awake_until, _relay_busy_until
    now = int(time.time())
    if now < _discovery_awake_until:
        return False
    if now < _relay_busy_until:
        return False
    if not tx_queue.empty():
        return False
    return True

def calculate_slot_jitter_ms(local_mac=None, tier=1):
    """
    Calculates deterministic micro-jitter slot delay (ms) based on MAC and mesh tier.
    - Tier 2 (Leaves): 0 .. 1400ms (transmit first to relays)
    - Tier 1 (Relays/Direct): 1800 .. 3200ms (relay leaf traffic, then transmit to Hub)
    """
    import random
    if not local_mac:
        try:
            sta = network.WLAN(network.STA_IF)
            local_mac = bytes_to_mac(sta.config('mac'))
        except Exception:
            local_mac = "00:00:00:00:00:00"
    
    try:
        mac_hash = sum(int(b, 16) for b in local_mac.split(':'))
    except Exception:
        mac_hash = random.randint(0, 100)

    if tier >= 2:
        slot_idx = mac_hash % 12
        jitter_ms = (slot_idx * 110) + random.randint(0, 80)
    else:
        slot_idx = mac_hash % 12
        jitter_ms = 1800 + (slot_idx * 110) + random.randint(0, 80)
        
    return jitter_ms

def get_next_wake_delay_ms(default_sec=30):
    """
    Returns closed-loop sleep duration (ms) synced with Hub's global mesh epoch.
    """
    global _next_wake_delay_ms
    if _next_wake_delay_ms > 0:
        val = _next_wake_delay_ms
        _next_wake_delay_ms = 0
        return max(5000, min(val, 120000))
    return default_sec * 1000

def client_tx_loop():
    global _e, _stop_requested
    print(" ESP-NOW Client TX Loop Thread Started")
    while not _stop_requested:
        try:
            item = tx_queue.get()
            if item is None:
                time.sleep_ms(50)  # Yield CPU
                continue
            
            next_hop_bytes, payload_bytes, phys_mac, target_id = item
            
            if _e is None:
                time.sleep_ms(100)
                tx_queue.put(item)
                continue
                
            try:
                add_peer_safe(_e, next_hop_bytes)
                res = config.send_fragmented(_e, next_hop_bytes, payload_bytes)
                print(f" [TX Queue] Envelope sent to next hop {phys_mac} for destination {target_id} (res={res})")
                try:
                    print(payload_bytes[2:].decode('utf-8'))
                except Exception:
                    pass
                print()
            except Exception as send_err:
                print(" [TX Queue] ESP-NOW send error:", send_err)
                if "buffer error" in str(send_err):
                    try:
                        _e.active(False)
                    except:
                        pass
                    time.sleep_ms(50)
                    try:
                        _e.active(True)
                    except:
                        pass
            
            import gc
            gc.collect()
            time.sleep_ms(50)
            
        except Exception as loop_err:
            print(" [TX Queue] Error in tx loop:", loop_err)
            time.sleep_ms(100)

def mac_to_bytes(mac_str):
    if not is_valid_mac(mac_str):
        return b'\xff\xff\xff\xff\xff\xff'
    try:
        return bytes(int(x, 16) for x in mac_str.split(':'))
    except Exception:
        return b'\xff\xff\xff\xff\xff\xff'

def bytes_to_mac(mac_bytes):
    return ':'.join('%02x' % b for b in mac_bytes)


def extract_complete_frame(buf):
    """Return (frame_bytes, remainder) if a full frame is available, otherwise (None, buf)."""
    if len(buf) < 2:
        return None, buf
    
    # Handle direct un-framed JSON payloads (e.g. b'{"pld"...')
    if buf[0] == 0x7b:
        return buf, b""

    frame_len = int.from_bytes(buf[:2], 'big')
    total_len = 2 + frame_len
    if len(buf) < total_len:
        return None, buf
    return buf[2:total_len], buf[total_len:]


def get_hub_id():
    cfg = config.load_config()
    hub_cfg = cfg.get("hub", {})
    if isinstance(hub_cfg, dict):
        for key in ("id", "node_id", "client_id"):
            hub_id = hub_cfg.get(key)
            if hub_id:
                return str(hub_id)

    return "hub_master_01"

def is_paired():
    global _paired
    if not _paired:
        try:
            cfg = config.load_config()
            hub_mac = cfg.get("hub", {}).get("mac", "")
            if is_valid_mac(hub_mac) and hub_mac != "00:00:00:00:00:00" and hub_mac != "ff:ff:ff:ff:ff:ff":
                _paired = True
        except Exception:
            pass
    return _paired

def set_paired(val):
    global _paired
    _paired = val

def stop_client():
    global _stop_requested, _paired, _e
    _stop_requested = True
    _paired = False
    if _e is not None:
        try:
            _e.active(False)
        except Exception:
            pass
    print(" ESP-NOW Client stopped")

def add_peer_safe(e, peer_bytes, channel=0):
    """Add/update an ESP-NOW peer. add_peer is idempotent and safe to call multiple times."""
    peer_bytes = bytes(peer_bytes)
    try:
        e.add_peer(peer_bytes, b'', channel, network.STA_IF)
    except OSError as ose:
        # Ignore 'ESP-NOW peer already exists' error, which is expected.
        err = ose.args[0] if ose.args else None
        if err not in (23, 12293, 12395, -12395) and 'ESP_ERR_ESPNOW_EXIST' not in str(ose):
            print(f"add_peer_safe notice: {ose}")

def parse_packet(payload_str):
    p = ujson.loads(payload_str)
    msg_type = p.get("msg_type") or p.get("t")
    target = p.get("target") or p.get("dst", "")
    source = p.get("source") or p.get("src")
    
    route = p.get("route") or p.get("rt", {})
    hops = (route.get("hops") or route.get("h")) if isinstance(route, dict) else []
    if not hops:
        hops = p.get("routing_path") or p.get("path") or []
        
    current_hop_index = (route.get("current_hop_index") or route.get("chi")) if isinstance(route, dict) else 0
    if current_hop_index == 0:
        current_hop_index = p.get("current_hop_index") or p.get("hop", 0)
        
    data = p.get("data") or p.get("payload") or p.get("pld", {})
    return {
        "msg_type": msg_type,
        "target": target,
        "source": source,
        "route": route,
        "hops": hops,
        "current_hop_index": current_hop_index,
        "data": data,
        "raw": p
    }

def is_valid_mac(mac_str):
    if not isinstance(mac_str, str) or len(mac_str) != 17 or mac_str.count(':') != 5:
        return False
    try:
        for part in mac_str.split(':'):
            if len(part) != 2:
                return False
            int(part, 16)
        return True
    except Exception:
        return False

def get_route_for_target(target_id, target_mac=None):
    cfg = config.load_config()
    
    # Check if there is a pre-provisioned route in config for this target
    routes = cfg.get("routes", {})
    if target_id in routes:
        route_info = routes[target_id]
        return route_info.get("route_id", "pre_provisioned"), route_info.get("hops", [])
        
    # Dynamic fallback to parent/hub configuration
    hub_cfg = cfg.get("hub", {})
    hub_mac = hub_cfg.get("mac", "ff:ff:ff:ff:ff:ff")
    
    parent_cfg = cfg.get("parent", {})
    parent_mac = parent_cfg.get("mac", "00:00:00:00:00:00")
    
    dest_mac = target_mac or hub_mac
    
    hops = []
    if is_valid_mac(parent_mac) and parent_mac != "00:00:00:00:00:00" and parent_mac != "ff:ff:ff:ff:ff:ff":
        hops.append(parent_mac)
    if is_valid_mac(dest_mac) and dest_mac not in hops:
        hops.append(dest_mac)
        
    if not hops:
        hops = [dest_mac]
        
    return "df", hops

def send_ack_or_tele_to_hub(msg_type, payload, target_mac=None):
    global _e
    if _e is None:
        return False

    cfg = config.load_config()
    client_cfg = cfg.get("client", {})
    source_id = client_cfg.get("id", "unknown_node")
    broadcast_only = client_cfg.get("espnow_broadcast_only", False)

    # Only an explicit broadcast destination uses broadcast routing. A pairing
    # request can be a unicast STATUS packet when a hub MAC is configured.
    is_broadcast = (broadcast_only or target_mac == "ff:ff:ff:ff:ff:ff")
    target_id = "broadcast" if is_broadcast else get_hub_id()

    # Get pre-provisioned route or dynamic fallback
    route_id, hops = get_route_for_target(target_id, target_mac)
    if is_broadcast:
        hops = ["ff:ff:ff:ff:ff:ff"]

    # Include local STA MAC in payload so Hub can accurately resolve origin and build return route
    sta = network.WLAN(network.STA_IF)
    local_mac = bytes_to_mac(sta.config('mac'))
    if isinstance(payload, dict):
        payload.setdefault("mac", local_mac)
        payload.setdefault("node_mac", local_mac)

    envelope = message_builder.build_espnow_envelope(
        source_id,
        target_id,
        "STATUS" if msg_type == "PAIR_REQ" else msg_type,
        payload,
        route_id=route_id,
        hops=hops
    )

    # Use next-hop MAC from routing path if available, fallback to target_mac, then broadcast
    phys_mac = "ff:ff:ff:ff:ff:ff" if broadcast_only else ((hops[0] if hops else target_mac) or "ff:ff:ff:ff:ff:ff")
    next_hop_bytes = mac_to_bytes(phys_mac)

    try:
        payload_str = config.compact_json(envelope)
        frame_bytes = config.make_frame(payload_str)
        tx_queue.put((next_hop_bytes, frame_bytes, phys_mac, target_id))
        return True
    except Exception as err:
        print(f" Failed to enqueue packet to destination {target_id}:", err)
        return False

# Backward compatibility alias for pump controller
def send_to_hub(msg_type, payload):
    return send_ack_or_tele_to_hub(msg_type, payload)

_pair_channel_idx = 0

def send_direct_espnow(target_mac_str, target_id, msg_type, payload):
    global _e
    if _e is None:
        return False

    cfg = config.load_config()
    source_id = cfg.get("client", {}).get("id", "unknown_node")

    envelope = message_builder.build_espnow_envelope(
        source_id,
        target_id,
        msg_type,
        payload,
        route_id="direct",
        hops=[target_mac_str]
    )

    next_hop_bytes = mac_to_bytes(target_mac_str)
    try:
        payload_str = config.compact_json(envelope)
        frame_bytes = config.make_frame(payload_str)
        tx_queue.put((next_hop_bytes, frame_bytes, target_mac_str, target_id))
        return True
    except Exception as err:
        print(f" Failed to send direct packet to {target_id}:", err)
        return False

def passive_beacon_scan():
    """
    Passive Beacon Scanning (Used during BLE Provisioning / Initial Join):
    Cycles through channels [4, 6, 1, 11] dwelling 4s per channel without transmitting.
    Listens for BEACON frames emitted by the Hub and active relay nodes.
    Locks channel and gathers candidate beacons to select the best parent.
    """
    global _e, _pair_channel_idx, _last_pairing_tx_time, _discovery_lock_channel, _candidate_settle_until, _candidate_beacons, _paired
    if _e is None:
        return False

    # If locked onto a beacon's channel, wait for candidate beacons to settle before deciding
    if _discovery_lock_channel is not None:
        if time.time() < _candidate_settle_until:
            return False
        if _candidate_beacons:
            best = min(_candidate_beacons.values(), key=lambda b: (b.get("hop_count", 99), b.get("rssi_rank", 0)))
            print(f" Best parent selected from Beacons: {best['parent_mac']} (Hops to Hub: {best['hop_count']}) on Channel {best['channel']}")
            config.update_config({
                "hub": {"mac": best["hub_mac"]},
                "parent": {"mac": best["parent_mac"]},
                "wifi": {"channel": best["channel"]}
            })
            _candidate_beacons.clear()
            _discovery_lock_channel = None
            _paired = True
            return True
        _discovery_lock_channel = None

    # Enforce a 4-second channel dwell time for passive listening
    if time.time() - _last_pairing_tx_time < 4:
        return False
    _last_pairing_tx_time = time.time()

    # Multi-Channel Passive Scanning: cycle channels (4, 6, 1, 11) to listen for Beacons
    channels = [4, 6, 1, 11]
    ch = channels[_pair_channel_idx % len(channels)]
    _pair_channel_idx += 1
    set_wifi_channel(ch)
    print(f" [Mesh Discovery] Scanning Channel {ch} (Passive Beacon Listening, 4s dwell)...")
    return False

def send_recovery_probe():
    """
    Active Recovery Probe (Used ONLY when a provisioned node loses contact with parent/hub):
    Cycles through channels [4, 6, 1, 11] (4s dwell) broadcasting DISCOVERY_REQ.
    Awake Hub and Relay nodes respond with DISCOVERY_RESP/BEACON.
    """
    global _e, _pair_channel_idx, _last_hub_rx_time, _last_pairing_tx_time, _discovery_lock_channel, _candidate_settle_until, _candidate_beacons, _paired
    if _e is None:
        return False

    # If locked onto a response channel, wait for candidate responses to settle before deciding
    if _discovery_lock_channel is not None:
        if time.time() < _candidate_settle_until:
            return False
        if _candidate_beacons:
            best = min(_candidate_beacons.values(), key=lambda b: (b.get("hop_count", 99), b.get("rssi_rank", 0)))
            print(f" Best parent selected from Recovery responses: {best['parent_mac']} (Hops to Hub: {best['hop_count']}) on Channel {best['channel']}")
            config.update_config({
                "hub": {"mac": best["hub_mac"]},
                "parent": {"mac": best["parent_mac"]},
                "wifi": {"channel": best["channel"]}
            })
            _candidate_beacons.clear()
            _discovery_lock_channel = None
            _paired = True
            return True
        _discovery_lock_channel = None

    # Enforce a 4-second channel dwell time on active recovery requests
    if time.time() - _last_pairing_tx_time < 4:
        return False
    _last_pairing_tx_time = time.time()

    cfg = config.load_config()
    client_cfg = cfg.get("client", {})
    source_id = client_cfg.get("id", "unknown_node")
    node_type = client_cfg.get("type", "client").upper()

    sta = network.WLAN(network.STA_IF)
    local_mac = bytes_to_mac(sta.config('mac'))

    payload = {
        "status": "recovery_probe",
        "node_type": node_type,
        "node_id": source_id,
        "custom_name": client_cfg.get("custom_name", "Client Node"),
        "mac": local_mac
    }

    channels = [4, 6, 1, 11]
    ch = channels[_pair_channel_idx % len(channels)]
    _pair_channel_idx += 1
    set_wifi_channel(ch)
    print(f" [Mesh Recovery] Scanning Channel {ch}: Broadcasting DISCOVERY_REQ from {node_type} (4s dwell)...")

    envelope = message_builder.build_espnow_envelope(
        source_id,
        "broadcast",
        "DISCOVERY_REQ",
        payload,
        route_id="recovery",
        hops=["ff:ff:ff:ff:ff:ff"]
    )

    try:
        payload_str = config.compact_json(envelope)
        frame_bytes = config.make_frame(payload_str)
        tx_queue.put((b'\xff\xff\xff\xff\xff\xff', frame_bytes, "ff:ff:ff:ff:ff:ff", "broadcast"))
        return True
    except Exception as err:
        print(" Failed to enqueue recovery probe packet:", err)
        return False

# Backward compatibility aliases
def send_discovery_request():
    return passive_beacon_scan()

def send_pairing_request():
    return send_recovery_probe()

def is_paired():
    global _paired
    if _paired:
        return True
    cfg = config.load_config()
    hub_mac = cfg.get("hub", {}).get("mac", "") or cfg.get("parent", {}).get("mac", "")
    if is_valid_mac(hub_mac) and hub_mac not in ("00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"):
        _paired = True
        return True
    return False

def init_espnow_client(on_cmd_received_fn=None):
    global _e, _stop_requested
    if not has_espnow:
        print(" ESP-NOW not supported on this firmware build.")
        return None

    cfg = config.load_config()
    _stop_requested = False
    client_name = cfg.get("client", {}).get("custom_name", "Client Node")
    print(f" Initializing {client_name} ESP-NOW Client...")
    
    # Deactivate AP interface to force ESP-NOW to bind to STA interface
    try:
        ap = network.WLAN(network.AP_IF)
        ap.active(False)
    except:
        pass
        
    ch = cfg.get("wifi", {}).get("channel", 4)
    set_wifi_channel(ch)
    
    _e = espnow.ESPNow()
    _e.active(True)
    try:
        _e.config(rxbuf=4096)
    except:
        pass
    
    espnow_relay.init_relay_engine(_e, lambda next_hop_bytes, payload_bytes, phys_mac, target_id: tx_queue.put((next_hop_bytes, payload_bytes, phys_mac, target_id)))
    
    # If not paired, start passive beacon scan
    if not is_paired():
        passive_beacon_scan()
    else:
        hub_mac = cfg.get("hub", {}).get("mac", "") or cfg.get("parent", {}).get("mac", "")
        print(f" Node paired with Hub/Parent {hub_mac} on Channel {ch}")
        
    return _e

def client_listen_loop(heartbeats=None, on_cmd_received_fn=None):
    global _e, _paired, _last_hub_rx_time, _next_wake_delay_ms, _discovery_awake_until, _last_beacon_broadcast_time, _discovery_lock_channel, _candidate_settle_until, _candidate_beacons
    if _e is None:
        return

    sta = network.WLAN(network.STA_IF)
    local_mac = bytes_to_mac(sta.config('mac'))

    cfg = config.load_config()
    client_cfg = cfg.get("client", {})
    local_id = client_cfg.get("id", "").lower()

    _last_hub_rx_time = time.time()
    recv_buffers = {}
    recv_last_seen = {}

    while not _stop_requested:
        if heartbeats is not None:
            heartbeats["esp_now"] = time.time()

        # Fallback to recovery if we lose contact with our paired Hub for 45s
        if _paired and time.time() - _last_hub_rx_time > 45:
            print(" Lost contact with Hub for 45s. Re-entering Recovery Mode...")
            _paired = False

        if not _paired:
            current_cfg = config.load_config()
            parent_mac = current_cfg.get("parent", {}).get("mac", "00:00:00:00:00:00")
            mode = current_cfg.get("client", {}).get("mode", "ble_setup")
            
            # If unprovisioned / in BLE setup without a valid parent: passive beacon scanning
            if not is_valid_mac(parent_mac) or parent_mac in ("00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff") or mode in ("ble_setup", "pending_confirm"):
                passive_beacon_scan()
            else:
                # Provisioned node that lost contact with parent/hub: active recovery probing
                send_recovery_probe()
        else:
            # Cascaded Beacon Re-broadcasting ONLY by dedicated Relay/Repeater Nodes during Discovery Window
            current_cfg = config.load_config()
            client_cfg = current_cfg.get("client", {})
            client_mode = client_cfg.get("mode", "ble_setup")
            is_relay = client_cfg.get("is_relay", False) or client_cfg.get("type", "").lower() in ("repeater", "relay")
            
            if is_relay and client_mode == "normal" and int(time.time()) < _discovery_awake_until:
                if time.time() - _last_beacon_broadcast_time >= 4.0:
                    _last_beacon_broadcast_time = time.time()
                    p_mac = current_cfg.get("parent", {}).get("mac", "")
                    h_mac = current_cfg.get("hub", {}).get("mac", "")
                    ch = current_cfg.get("wifi", {}).get("channel", 6)
                    my_hops = 1 if (p_mac == h_mac or not is_valid_mac(p_mac)) else 2
                    send_direct_espnow(
                        target_mac_str="ff:ff:ff:ff:ff:ff",
                        target_id="broadcast",
                        msg_type="BEACON",
                        payload={
                            "hub_mac": h_mac,
                            "sender_mac": local_mac,
                            "channel": ch,
                            "hop_count": my_hops,
                            "valid_until": _discovery_awake_until
                        }
                    )

        try:
            host, msg = _e.recv(500)
            if host and msg:
                # Ignore tiny non-JSON fragments that can appear during ESP-NOW
                # fragmentation and poison the receive buffer.
                if len(msg) < 8 and b'{' not in msg:
                    continue

                sender_mac = bytes_to_mac(host)
                # Cleanup stale peer buffer if contact gap > 10s
                now_t = time.time()
                if now_t - recv_last_seen.get(sender_mac, now_t) > 10:
                    recv_buffers[sender_mac] = b""
                recv_last_seen[sender_mac] = now_t

                buf = recv_buffers.get(sender_mac, b"") + msg
                recv_buffers[sender_mac] = buf

                while True:
                    payload_bytes, remainder = extract_complete_frame(recv_buffers[sender_mac])
                    if payload_bytes is None:
                        if len(recv_buffers[sender_mac]) > MAX_RX_BUFFER:
                            recv_buffers[sender_mac] = b""
                        break

                    recv_buffers[sender_mac] = remainder
                    try:
                        payload_str = payload_bytes.decode('utf-8')
                    except Exception as decode_err:
                        print(f"  Ignoring non-UTF-8 payload from {sender_mac}: {decode_err}")
                        continue
                    print(f" Received packet from {sender_mac}: {payload_str}")

                    packet = parse_packet(payload_str)
                    
                    # Sync RTC if envelope has a valid Hub timestamp
                    hub_ts = packet.get("timestamp") or packet.get("ts")
                    if hub_ts and isinstance(hub_ts, (int, float)) and hub_ts > 1700000000:
                        local_ts = config.get_unix_time()
                        if abs(local_ts - hub_ts) > 5:
                            try:
                                import machine
                                tm = time.gmtime(hub_ts - 946684800)
                                rtc = machine.RTC()
                                rtc.datetime((tm[0], tm[1], tm[2], tm[6], tm[3], tm[4], tm[5], 0))
                                print(f" RTC synchronized via ESP-NOW from Hub to: {time.localtime()}")
                            except Exception as sync_ex:
                                print(" Failed to sync RTC via ESP-NOW:", sync_ex)

                    msg_type = packet.get("msg_type")
                    target = packet.get("target")

                    is_for_us = False
                    if target:
                        t_lower = target.lower()
                        is_for_us = (t_lower == local_id or t_lower in ("broadcast", "ff:ff:ff:ff:ff:ff"))
                    else:
                        is_for_us = (msg_type == "CMD" or msg_type == "ACK" or msg_type == "COMMAND")

                    # Handle BEACON packets
                    if msg_type == "BEACON":
                        b_pld = packet.get("data") or packet.get("pld", {})
                        hub_mac = b_pld.get("hub_mac", sender_mac)
                        parent_mac = b_pld.get("sender_mac", sender_mac)
                        b_ch = b_pld.get("channel")
                        hop_count = b_pld.get("hop_count", 0)
                        valid_until = b_pld.get("valid_until", 0)

                        current_cfg = config.load_config()
                        paired_hub = current_cfg.get("hub", {}).get("mac", "")
                        is_our_hub = (hub_mac.lower().replace(':', '') == paired_hub.lower().replace(':', ''))

                        now_unix = config.get_unix_time()
                        # If beacon has active validity, engage discovery awake lock
                        if valid_until > now_unix or (valid_until > 0 and valid_until > int(time.time())):
                            awake_sec = max(10, min(300, valid_until - now_unix if valid_until > now_unix else valid_until - int(time.time())))
                            _discovery_awake_until = max(_discovery_awake_until, int(time.time()) + awake_sec)
                            print(f" [Mesh Discovery] Discovery mode active! Holding radio awake for {awake_sec}s (hop={hop_count})")

                        if _paired:
                            _last_hub_rx_time = time.time()
                            if is_our_hub and b_ch and current_cfg.get("wifi", {}).get("channel") != b_ch:
                                upd = {"wifi": {"channel": b_ch}}
                                set_wifi_channel(b_ch)
                                config.update_config(upd)
                        else:
                            # Un-paired node receiving beacon: Lock channel and record candidate!
                            if b_ch:
                                if _discovery_lock_channel != b_ch:
                                    _discovery_lock_channel = b_ch
                                    _candidate_settle_until = time.time() + 3
                                    set_wifi_channel(b_ch)
                                    print(f" [Mesh Discovery] Beacon detected! Locked to Channel {b_ch}. Gathering candidates for 3s...")
                                
                                _candidate_beacons[parent_mac] = {
                                    "hub_mac": hub_mac,
                                    "parent_mac": parent_mac,
                                    "channel": b_ch,
                                    "hop_count": hop_count,
                                    "rssi_rank": 0
                                }

                    if msg_type == "ACK" and is_for_us:
                        ack_pld = packet.get("data") or packet.get("pld", {})
                        if ack_pld.get("status") == "paired":
                            _paired = True
                            _last_hub_rx_time = time.time()
                            hub_mac = ack_pld.get("hub_mac", sender_mac)
                            hub_ch = ack_pld.get("channel")
                            print(f"Client paired successfully with Hub ({hub_mac}) on Channel {hub_ch}!")
                            try:
                                current_parent = current_cfg.get("parent", {}).get("mac", "")
                                parent_to_save = current_parent if (is_valid_mac(current_parent) and current_parent != "00:00:00:00:00:00" and current_parent != "ff:ff:ff:ff:ff:ff" and current_parent != hub_mac) else hub_mac
                                upd = {"hub": {"mac": hub_mac}, "parent": {"mac": parent_to_save}}
                                if hub_ch:
                                    upd["wifi"] = {"channel": hub_ch}
                                    set_wifi_channel(hub_ch)
                                config.update_config(upd)
                            except Exception as ex:
                                print("Error updating config on pairing:", ex)

                    # Handle Route/Parent Discovery packets
                    if msg_type == "DISCOVERY_REQ":
                        current_cfg = config.load_config()
                        client_mode = current_cfg.get("client", {}).get("mode", "ble_setup")
                        if _paired and client_mode == "normal" and (time.time() - _last_hub_rx_time < 60):
                            sender_mac = bytes_to_mac(host)
                            hub_mac = current_cfg.get("hub", {}).get("mac", "")
                            channel = current_cfg.get("wifi", {}).get("channel", 6)
                            
                            parent_mac = current_cfg.get("parent", {}).get("mac", "")
                            hop_count = 1 if parent_mac == hub_mac else 2
                            
                            resp_payload = {
                                "status": "discovery_response",
                                "hub_mac": hub_mac,
                                "parent_mac": local_mac,
                                "channel": channel,
                                "hop_count": hop_count,
                                "hub_freshness": int(time.time() - _last_hub_rx_time)
                            }
                            
                            print(f" Received DISCOVERY_REQ from {sender_mac}. Replying with DISCOVERY_RESP...")
                            send_direct_espnow(
                                target_mac_str=sender_mac,
                                target_id=packet.get("source") or sender_mac,
                                msg_type="DISCOVERY_RESP",
                                payload=resp_payload
                            )
                        continue

                    if msg_type == "DISCOVERY_RESP" and is_for_us:
                        resp_data = packet.get("data") or packet.get("pld", {})
                        hub_mac = resp_data.get("hub_mac")
                        parent_mac = resp_data.get("parent_mac")
                        ch = resp_data.get("channel")
                        hop_count = resp_data.get("hop_count")
                        hub_freshness = resp_data.get("hub_freshness", 999)
                        
                        print(f" Received DISCOVERY_RESP from {sender_mac} (Hub={hub_mac}, Channel={ch}, Hops={hop_count})")
                        
                        try:
                            upd = {
                                "hub": {"mac": hub_mac},
                                "parent": {"mac": parent_mac},
                                "wifi": {"channel": ch}
                            }
                            config.update_config(upd)
                            set_wifi_channel(ch)
                            
                            _paired = True
                            _last_hub_rx_time = time.time() - hub_freshness
                            print(f" Discovered route: parent={parent_mac}, hub={hub_mac}, locked to channel {ch}")
                        except Exception as ex:
                            print(" Error saving discovered route:", ex)
                        continue

                    # Relaying and target validation
                    is_actually_for_us = espnow_relay.process_and_relay(packet)

                    # Update RX timestamp if packet is from Hub
                    current_cfg = config.load_config()
                    paired_hub = current_cfg.get("hub", {}).get("mac", "")
                    if sender_mac.lower().replace(':', '') == paired_hub.lower().replace(':', ''):
                        _last_hub_rx_time = time.time()

                    if is_actually_for_us:
                        payload = packet.get("data") or packet.get("pld", {})
                        if isinstance(payload, dict):
                            # Closed-loop sleep synchronization
                            delay_ms = payload.get("next_wake_delay_ms")
                            if delay_ms is not None:
                                try:
                                    _next_wake_delay_ms = int(delay_ms)
                                    if current_cfg.get("client", {}).get("deep_sleep_enabled", True):
                                        print(f" [Sleep Sync] Closed-loop sleep sync from Hub: {_next_wake_delay_ms}ms")
                                except Exception:
                                    pass

                        if on_cmd_received_fn is not None:
                            if msg_type in ("COMMAND", "CMD"):
                                cmd = payload.get("cmd") or payload.get("command")
                                payload["sender_mac"] = sender_mac
                                payload["routing_path"] = packet.get("hops", [])
                                on_cmd_received_fn(cmd, payload)
                            elif msg_type == "ACK":
                                on_cmd_received_fn(None, payload)
            else:
                time.sleep_ms(50)

        except Exception as err:
            err_str = str(err)
            if "buffer error" in err_str:
                try:
                    _e.active(False)
                except:
                    pass
                time.sleep_ms(50)
                try:
                    _e = espnow.ESPNow()
                    _e.active(True)
                except:
                    pass
            else:
                print("Client loop error:", err)
            time.sleep_ms(100)

    try:
        _e.active(False)
    except Exception:
        pass
    print(" ESP-NOW listener stopped")
