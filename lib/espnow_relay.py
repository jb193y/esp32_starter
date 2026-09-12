# espnow_relay.py (Shared Relay Engine Library)
import ujson
import network
try:
    import espnow
except ImportError:
    espnow = None
import config

_e = None
_enqueue_fn = None

def is_valid_mac(mac_str):
    if not isinstance(mac_str, str):
        return False
    parts = mac_str.split(':')
    if len(parts) != 6:
        return False
    try:
        for p in parts:
            if len(p) != 2:
                return False
            int(p, 16)
        return True
    except ValueError:
        return False

def mac_to_bytes(mac_str):
    return bytes(int(x, 16) for x in mac_str.split(':'))

def bytes_to_mac(mac_bytes):
    return ':'.join('%02x' % b for b in mac_bytes)

def init_relay_engine(espnow_instance, enqueue_fn=None):
    global _e, _enqueue_fn
    _e = espnow_instance
    _enqueue_fn = enqueue_fn

def add_peer_safe(e, peer_bytes, channel=0):
    """Add ESP-NOW peer, registering on both STA_IF and AP_IF to support concurrent reception."""
    peer_bytes = bytes(peer_bytes)
    import network

    # Register on STA interface
    try:
        e.del_peer(peer_bytes)
    except:
        pass
    try:
        e.add_peer(peer_bytes, b'', channel, network.STA_IF)
    except:
        pass

    # Register on AP interface
    try:
        e.add_peer(peer_bytes, b'', channel, network.AP_IF)
    except:
        pass

def parse_packet(payload_str):
    p = ujson.loads(payload_str)
    msg_type = p.get("msg_type") or p.get("t")
    target = p.get("target") or p.get("dst", "")
    source = p.get("source") or p.get("src")
    
    route = p.get("route") or p.get("rt", {})
    hops = route.get("hops") if isinstance(route, dict) else []
    if not hops:
        hops = p.get("routing_path") or p.get("path") or []
        
    current_hop_index = route.get("current_hop_index") if isinstance(route, dict) else 0
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

def process_and_relay(packet):
    """
    Evaluates packet routing.
    Returns: True if packet target is us, False if packet was relayed or ignored.
    """
    global _e
    if _e is None:
        print(" Relay engine not initialized with ESPNow")
        return False

    msg_type = packet.get("msg_type")
    target = packet.get("target")
    route = packet.get("route") or packet.get("rt") or {}
    hops = packet.get("hops", [])
    if not hops and isinstance(route, dict):
        hops = route.get("hops", [])
    if not isinstance(hops, list):
        hops = []

    try:
        current_hop_index = int(packet.get("current_hop_index", route.get("current_hop_index", 0)))
    except Exception:
        current_hop_index = 0

    sta = network.WLAN(network.STA_IF)
    local_mac = bytes_to_mac(sta.config('mac'))

    cfg = config.load_config()
    client_cfg = cfg.get("client", {})
    local_id = client_cfg.get("id", "").lower()

    # 1. Check if packet target is us (case-insensitive) or broadcast
    if target:
        t_lower = target.lower()
        if (t_lower == local_id or 
            t_lower == local_mac.replace(':', '').lower() or 
            t_lower == local_mac.lower()):
            print(" Packet reached final target destination.")
            return True
        if t_lower in ("broadcast", "ff:ff:ff:ff:ff:ff"):
            return True

    # 2. Check if we should relay it based on current_hop_index and hops
    if not hops:
        return False

    if current_hop_index < 0:
        current_hop_index = 0
    if current_hop_index >= len(hops):
        print(" current_hop_index out of hops bounds")
        return False

    current_hop_mac = hops[current_hop_index]

    # If the current hop MAC matches us, we need to relay to next hop
    if current_hop_mac and current_hop_mac.upper() == local_mac.upper():
        next_hop_index = current_hop_index + 1
        if next_hop_index >= len(hops):
            print(" Routing error: no next hop and we are not the target.")
            return False

        # If next hop is the Hub, but this relay itself has an upstream parent, dynamically expand hops
        p_mac = cfg.get("parent", {}).get("mac", "")
        h_mac = cfg.get("hub", {}).get("mac", "")
        if (hops[next_hop_index].lower() == h_mac.lower() and 
            is_valid_mac(p_mac) and 
            p_mac.lower() != h_mac.lower() and 
            p_mac.lower() != local_mac.lower()):
            hops.insert(next_hop_index, p_mac)

        next_hop_mac = hops[next_hop_index]
        next_hop_bytes = mac_to_bytes(next_hop_mac)

        # Update the packet in-place with new current_hop_index in raw dict
        if "raw" in packet and isinstance(packet["raw"], dict):
            raw_packet = packet["raw"]
            route_obj = raw_packet.get("route") or raw_packet.get("rt")
            if isinstance(route_obj, dict):
                if "chi" in route_obj or "rt" in raw_packet:
                    route_obj["chi"] = next_hop_index
                    route_obj["h"] = hops
                else:
                    route_obj["current_hop_index"] = next_hop_index
                    route_obj["hops"] = hops
            else:
                raw_packet["current_hop_index"] = next_hop_index
                raw_packet["route"] = {"current_hop_index": next_hop_index, "hops": hops}
                if "rt" in raw_packet:
                    raw_packet["rt"] = raw_packet["route"]

        print(f" Relaying packet to next hop: {next_hop_mac}")
        try:
            import espnow_client
            espnow_client.touch_relay_activity(30)
        except Exception:
            pass
        try:
            payload_str = ujson.dumps(packet["raw"])
            if _enqueue_fn is not None:
                _enqueue_fn(next_hop_bytes, config.make_frame(payload_str), next_hop_mac, packet.get("dst", "unknown"))
            else:
                cfg = config.load_config()
                client_cfg = cfg.get("client", {})
                max_retries = int(client_cfg.get("espnow_max_retries", 3))
                retry_delay = int(client_cfg.get("espnow_retry_delay_ms", 50))
                add_peer_safe(_e, next_hop_bytes)
                config.send_fragmented(_e, next_hop_bytes, config.make_frame(payload_str), max_retries=max_retries, retry_delay_ms=retry_delay)
            print(" Packet relayed successfully.")
        except Exception as e:
            print(f" Failed to relay packet to next hop: {e}")

        return False

    return False
