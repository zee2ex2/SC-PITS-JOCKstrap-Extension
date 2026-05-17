import json
import threading
import urllib.parse
import urllib.request
import sqlite3


def community_api_request(method, endpoint, community_url, token=None, body=None, timeout=10):
    if not community_url:
        return None, "Community URL not configured"
    url = f"{community_url.rstrip('/')}/api/{endpoint}"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode()), None
    except urllib.error.HTTPError as e:
        detail = e.read().decode() if e.fp else ""
        return None, f"HTTP {e.code}: {detail}"
    except Exception as e:
        return None, str(e)


def push_inventory(db, community_url, api_key, inv_id, item_name, qual, qty_scu, station_name=None):
    body = {
        "inventory_id": inv_id,
        "item_name": item_name,
        "quality": qual,
        "quantity_scu": qty_scu,
        "station": station_name or "",
    }
    resp, err = community_api_request("POST", "inventory/sync", community_url, token=api_key, body=body)
    _log_sync(db, "push", "ok" if not err else "fail", err or f"Synced item {inv_id}")
    return resp, err


def delete_remote_inventory(db, community_url, api_key, inv_id):
    body = {"inventory_id": inv_id}
    resp, err = community_api_request("DELETE", "inventory/sync", community_url, token=api_key, body=body)
    _log_sync(db, "push", "ok" if not err else "fail", err or f"Deleted item {inv_id}")
    return resp, err


def pull_inventory(db, community_url, api_key):
    resp, err = community_api_request("GET", "inventory/sync", community_url, token=api_key)
    if resp and isinstance(resp, list):
        _log_sync(db, "pull", "ok", f"Pulled {len(resp)} items")
    else:
        _log_sync(db, "pull", "fail", err or "No data returned")
    return resp, err


def fetch_notifications(community_url, api_key):
    resp, err = community_api_request("GET", "notifications", community_url, token=api_key, timeout=8)
    return resp, err


def fetch_order_requests(community_url, api_key, status="open"):
    params = {"status": status}
    qs = urllib.parse.urlencode(params)
    resp, err = community_api_request("GET", f"orders?{qs}", community_url, token=api_key, timeout=8)
    return resp, err


def create_order_request(community_url, api_key, item_name, min_quality, quantity, notes=""):
    body = {
        "item_name": item_name,
        "min_quality": min_quality,
        "quantity": quantity,
        "notes": notes,
    }
    resp, err = community_api_request("POST", "orders", community_url, token=api_key, body=body)
    return resp, err


def fulfill_order(community_url, api_key, order_id):
    body = {"order_id": order_id}
    resp, err = community_api_request("POST", "orders/fulfill", community_url, token=api_key, body=body)
    return resp, err


def _log_sync(db, direction, status, message):
    try:
        db.execute(
            "INSERT INTO ext_sync_log (direction, status, message) VALUES (?, ?, ?)",
            (direction, status, message[:500]),
        )
        db.commit()
    except sqlite3.Error:
        pass


class SyncEngine:
    def __init__(self, globals_dict):
        self.g = globals_dict
        self._timer = None
        self._interval = 60

    def start(self):
        self._poll()

    def stop(self):
        if self._timer:
            self._timer.cancel()
            self._timer = None

    def _poll(self):
        try:
            self._poll_notifications()
        except Exception:
            pass
        self._timer = threading.Timer(self._interval, self._poll)
        self._timer.daemon = True
        self._timer.start()

    def _poll_notifications(self):
        config = self.g.get("ext_jock_config", {})
        community_url = config.get("community_url", "")
        api_key = config.get("api_key", "")
        if not community_url or not api_key:
            return
        notifs, err = fetch_notifications(community_url, api_key)
        if notifs and isinstance(notifs, list):
            from pathlib import Path
            from extensions.jock_strap import get_data_dir
            data_dir = get_data_dir()
            notif_file = data_dir / "notifications_cache.json"
            notif_file.parent.mkdir(parents=True, exist_ok=True)
            notif_file.write_text(json.dumps(notifs, indent=2))

    def get_cached_notifications(self):
        from pathlib import Path
        from extensions.jock_strap import get_data_dir
        notif_file = get_data_dir() / "notifications_cache.json"
        if notif_file.exists():
            try:
                return json.loads(notif_file.read_text())
            except (json.JSONDecodeError, OSError):
                return []
        return []
