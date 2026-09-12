# config.py (Shared Configuration Library)
import ujson
import os

CONFIG_FILE = "config.json"
DEFAULT_FILE = "config.defaults.json"

_cached_config = None

def _read_json(path):
    with open(path, "r") as f:
        return ujson.load(f)

def _write_json(path, obj):
    with open(path, "w") as f:
        ujson.dump(obj, f)

def _deep_merge(dst, src):
    for k, v in src.items():
        if k not in dst:
            dst[k] = v
        else:
            if isinstance(dst[k], dict) and isinstance(v, dict):
                _deep_merge(dst[k], v)
    return dst

def load_defaults():
    if DEFAULT_FILE not in os.listdir():
        return {}
    return _read_json(DEFAULT_FILE)

def load_config(force_reload=False):
    global _cached_config

    defaults = load_defaults()
    cfg = {}

    try:
        if force_reload or _cached_config is None:
            try:
                cfg = _read_json(CONFIG_FILE)
            except Exception:
                cfg = {}

            if not isinstance(cfg, dict):
                cfg = {}

            cfg = _deep_merge(cfg, defaults)
            _cached_config = cfg
            return cfg

        if CONFIG_FILE in os.listdir():
            latest_cfg = _read_json(CONFIG_FILE)
            if isinstance(latest_cfg, dict):
                _cached_config = _deep_merge(latest_cfg, defaults)
        return _cached_config
    except Exception:
        return _cached_config or _deep_merge({}, defaults)

def save_config(cfg):
    global _cached_config
    _cached_config = cfg
    _write_json(CONFIG_FILE, cfg)

def update_config(data):
    cfg = load_config()
    if not isinstance(data, dict):
        return cfg

    for group, patch in data.items():
        if isinstance(patch, dict) and isinstance(cfg.get(group), dict):
            cfg[group].update(patch)
        else:
            cfg[group] = patch

    save_config(cfg)
    return cfg

def get_unix_time():
    import time
    t = time.time()
    # MicroPython epoch (2000-01-01) to Unix epoch (1970-01-01) offset
    if t > 1000000:
        return t + 946684800
    return t

def get_unix_time_ms():
    import time
    sec = get_unix_time()
    try:
        ms = time.ticks_ms() % 1000
    except Exception:
        ms = 0
    return int(sec * 1000 + ms)

def make_frame(body):
    payload = body.encode('utf-8')
    return len(payload).to_bytes(2, 'big') + payload

class Queue:
    def __init__(self):
        import _thread
        self._queue = []
        self._lock = _thread.allocate_lock()
    
    def put(self, item):
        self._lock.acquire()
        try:
            self._queue.append(item)
        finally:
            self._lock.release()
    
    def get(self):
        self._lock.acquire()
        try:
            if self._queue:
                return self._queue.pop(0)
            return None
        finally:
            self._lock.release()
            
    def empty(self):
        self._lock.acquire()
        try:
            return len(self._queue) == 0
        finally:
            self._lock.release()

    def qsize(self):
        self._lock.acquire()
        try:
            return len(self._queue)
        finally:
            self._lock.release()

def compact_json(obj):
    import ujson
    s = ujson.dumps(obj)
    res = []
    in_string = False
    escaped = False
    for char in s:
        if char == '"' and not escaped:
            in_string = not in_string
            res.append(char)
        elif char == '\\' and in_string:
            escaped = not escaped
            res.append(char)
        else:
            escaped = False
            if not in_string and char in (' ', '\n', '\r', '\t'):
                continue
            res.append(char)
    return "".join(res)

def send_fragmented(e, peer_bytes, frame_bytes, chunk_size=240, delay_ms=25, max_retries=3, retry_delay_ms=50):
    import time
    total_len = len(frame_bytes)
    
    def _send_single_chunk(chunk):
        for attempt in range(1, max_retries + 1):
            try:
                res = e.send(peer_bytes, chunk)
                if res is not False:
                    return True
            except Exception as ex:
                pass
            if attempt < max_retries:
                time.sleep_ms(retry_delay_ms * attempt)
        return False

    if total_len <= chunk_size:
        return _send_single_chunk(frame_bytes)
        
    offset = 0
    while offset < total_len:
        chunk = frame_bytes[offset:offset+chunk_size]
        if not _send_single_chunk(chunk):
            return False
        offset += chunk_size
        if offset < total_len:
            time.sleep_ms(delay_ms)
            
    return True
