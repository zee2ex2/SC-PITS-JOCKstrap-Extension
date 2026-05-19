import json
import os
import ssl
import threading
import urllib.parse
import urllib.request
from pathlib import Path

import websocket

from extensions import Extension

AUTH_FILE = None
SYNC_SETTINGS_FILE = None


def get_data_dir():
    d = Path(os.environ.get("EXT_JOCK_DATA", str(Path.home() / ".config" / "sc-pits-jock")))
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_auth():
    global AUTH_FILE
    d = get_data_dir()
    AUTH_FILE = d / "auth.json"
    if AUTH_FILE.exists():
        try:
            return json.loads(AUTH_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_auth(data):
    if AUTH_FILE:
        AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        AUTH_FILE.write_text(json.dumps(data, indent=2))


def load_sync_settings():
    global SYNC_SETTINGS_FILE
    d = get_data_dir()
    SYNC_SETTINGS_FILE = d / "sync_settings.json"
    if SYNC_SETTINGS_FILE.exists():
        try:
            return json.loads(SYNC_SETTINGS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {"auto_sync": True, "community_url": ""}


def save_sync_settings(data):
    if SYNC_SETTINGS_FILE:
        SYNC_SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SYNC_SETTINGS_FILE.write_text(json.dumps(data, indent=2))


def community_api(method, endpoint, community_url, token=None, body=None, timeout=10):
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


def esc(val):
    if val is None:
        return ""
    import html as _html
    return _html.escape(str(val), quote=True)


class JockStrapExtension(Extension):
    name = "jock_strap"
    version = "1.0"
    repo_url = "zee2ex2/SC-PITS-JOCKstrap-Extension"
    description = "JOCK Strap — Discord OAuth via SHOWER, community sync, orders, notifications"

    def on_startup(self, g):
        self.g = g
        self.sync_settings = load_sync_settings()
        self._sync_engine = None
        self._ws = None
        self._ws_running = False
        self._ws_connected = False
        self._user_info = {}
        self._start_sync_engine()

    def _is_connected(self):
        return self._ws_connected

    def _get_token(self):
        return ""

    def _start_sync_engine(self):
        if self._sync_engine:
            return
        def poll():
            if self.sync_settings.get("auto_sync", False):
                self._poll_notifications()
            t = threading.Timer(60, poll)
            t.daemon = True
            t.start()
        t = threading.Timer(60, poll)
        t.daemon = True
        t.start()

    # --- WebSocket ---
    def _ws_connect(self, auth_code=None):
        self._ws_close()
        community_url = self.sync_settings.get("community_url", "")
        token = self._get_token()
        if not community_url:
            return
        if not auth_code and not token:
            return
        from urllib.parse import urlparse
        parsed = urlparse(community_url)
        host = parsed.hostname or "localhost"
        is_secure = parsed.scheme == "https"
        ws_scheme = "wss" if is_secure else "ws"
        ws_port = parsed.port or (443 if is_secure else 9200)
        ws_url = f"{ws_scheme}://{host}:{ws_port}"

        def _on_open(ws):
            if auth_code:
                ws.send(json.dumps({"type": "auth_code", "code": auth_code}))
            elif token:
                ws.send(json.dumps({"type": "auth", "token": token}))
            else:
                self._ws_running = False

        def _on_message(ws, message):
            try:
                data = json.loads(message)
                msg_type = data.get("type", "")
                if msg_type == "push_inventory":
                    self._handle_ws_push(data)
                elif msg_type == "auth_ok":
                    self._ws_connected = True
                    self._user_info = data.get("user", {})
                elif msg_type in ("auth_error", "disconnect"):
                    self._ws_connected = False
                    self._ws_running = False
            except Exception:
                pass

        def _run():
            self._ws_running = True
            while self._ws_running:
                try:
                    ws = websocket.WebSocketApp(ws_url,
                        on_open=_on_open,
                        on_message=_on_message,
                        on_error=lambda ws, e: setattr(self, '_ws_connected', False),
                        on_close=lambda ws, *a: setattr(self, '_ws_connected', False))
                    self._ws = ws
                    ws.run_forever(ping_interval=30, ping_timeout=10,
                                   sslopt={"cert_reqs": ssl.CERT_NONE} if is_secure else None)
                except Exception:
                    pass
                if self._ws_running:
                    import time
                    time.sleep(5)

        t = threading.Thread(target=_run, daemon=True)
        t.start()

    def _ws_close(self):
        self._ws_running = False
        ws = self._ws
        if ws:
            try:
                ws.close()
            except Exception:
                pass
            self._ws = None

    def _ws_send(self, data):
        ws = self._ws
        if ws and ws.sock and getattr(ws.sock, 'connected', False):
            try:
                ws.send(json.dumps(data))
                return True
            except Exception:
                pass
        return False

    def _handle_ws_push(self, data):
        store = self.g["store"]
        db = store.connect()
        try:
            action = data.get("action", "")
            itemid = data.get("itemid", "")
            if not itemid:
                return
            quality = int(data.get("quality", 100))
            quantity_scu = float(data.get("quantity_scu", 0))
            stationid = data.get("stationid", "")
            row = db.execute("SELECT id FROM item WHERE id=?", (int(itemid),)).fetchone()
            if not row:
                return
            if action == "add":
                qty_val = int(round(quantity_scu * 100))
                store.add_inventory(db, int(itemid), quality, qty_val, int(stationid) if stationid else None)
            elif action == "delete":
                qty_val = int(round(quantity_scu * 100))
                if stationid:
                    inv = db.execute(
                        "SELECT id FROM inventory WHERE itemid=? AND qual=? AND qty=? AND stationid=? ORDER BY id LIMIT 1",
                        (int(itemid), quality, qty_val, int(stationid))
                    ).fetchone()
                else:
                    inv = db.execute(
                        "SELECT id FROM inventory WHERE itemid=? AND qual=? AND qty=? AND stationid IS NULL ORDER BY id LIMIT 1",
                        (int(itemid), quality, qty_val)
                    ).fetchone()
                if inv:
                    store.delete_inventory(db, inv[0])
        except Exception:
            pass
        finally:
            db.close()

    def _poll_notifications(self):
        community_url = self.sync_settings.get("community_url", "")
        token = self._get_token()
        if not community_url or not token:
            return
        notifs, err = community_api("GET", "notifications", community_url, token=token, timeout=8)
        if notifs and isinstance(notifs, list):
            cache = get_data_dir() / "notifications_cache.json"
            cache.write_text(json.dumps(notifs, indent=2))

    def _cached_notifs(self):
        cache = get_data_dir() / "notifications_cache.json"
        if cache.exists():
            try:
                return json.loads(cache.read_text())
            except (json.JSONDecodeError, OSError):
                return []
        return []

    def _unread_count(self):
        return sum(1 for n in self._cached_notifs() if not n.get("read"))

    def get_context(self):
        logged_in = self._is_connected()
        unread = self._unread_count()
        badge = f'<span class="notif-badge">{unread}</span>' if unread > 0 else ""
        community_url = self.sync_settings.get("community_url", "")
        name = self._user_info.get("display_name") or self._user_info.get("username") or "User"
        shower_link = f'<a class="button ghost" href="{esc(community_url)}" target="_blank" id="jock-shower-link">ShoWER</a>' if community_url else ""
        theme_script = '<script>(function(){var a=document.getElementById("jock-shower-link");if(a){a.addEventListener("click",function(){var t=localStorage.getItem("theme");if(t)this.href=this.href.split("?")[0]+"?theme="+t;});}})();</script>' if community_url else ""
        title_suffix = '<span style="font-size:11px;font-weight:400;color:#a92a28;margin-left:8px">Connected with JOCKstrap</span>' if logged_in else ""
        nav = f"""
        <a class="button ghost" href="/ext/jock/orders">Orders</a>
        {shower_link}{theme_script}
        <div class="user-dropdown" id="jock-dropdown">
            <span class="dropdown-toggle button ghost" onclick="event.stopPropagation();document.getElementById('jock-dropdown').classList.toggle('open')">{name} &#9662;</span>
            <div class="dropdown-menu">
                <a class="button ghost" href="/ext/jock/notifications">Notifications{badge}</a>
            </div>
        </div>
        <script>
        ;(function(){{var d=document.getElementById('jock-dropdown');if(d)document.addEventListener('click',function(e){{if(!d.contains(e.target))d.classList.remove('open');}});}})();
        </script>
        """ if logged_in else ""
        return {
            "ext_jock_logged_in": str(logged_in).lower(),
            "ext_jock_tag": self._user_info.get("display_name", "") or self._user_info.get("discord_tag", ""),
            "ext_jock_display_name": self._user_info.get("display_name", ""),
            "ext_jock_guild_verified": "1" if logged_in else "0",
            "ext_jock_roles": "",
            "ext_jock_unread": str(unread),
            "ext_jock_community_url": self.sync_settings.get("community_url", ""),
            "ext_jock_connected": str(logged_in).lower(),
            "ext_status": "Connected" if logged_in else "Disconnected",
            "ext_status_cls": "ok" if logged_in else "error",
            "_nav_html": nav,
            "_title_suffix": title_suffix,
        }
        return {
            "ext_jock_logged_in": str(logged_in).lower(),
            "ext_jock_tag": self._user_info.get("display_name", "") or self._user_info.get("discord_tag", ""),
            "ext_jock_display_name": self._user_info.get("display_name", ""),
            "ext_jock_guild_verified": "1" if logged_in else "0",
            "ext_jock_roles": "",
            "ext_jock_unread": str(unread),
            "ext_jock_community_url": self.sync_settings.get("community_url", ""),
            "ext_jock_connected": str(logged_in).lower(),
            "ext_status": "Connected" if logged_in else "Disconnected",
            "ext_status_cls": "ok" if logged_in else "error",
            "_nav_html": nav,
        }

    def get_settings_html(self):
        logged_in = self._is_connected()
        tag = self._user_info.get("display_name") or self._user_info.get("discord_tag") or ""
        community_url = self.sync_settings.get("community_url", "")
        auto_sync = self.sync_settings.get("auto_sync", True)
        auto_checked = 'checked' if auto_sync else ''

        login_section = ""
        if logged_in:
            login_section = f"""
            <p style="margin:12px 0">Connected as <strong>{esc(tag)}</strong></p>
            <p style="margin:4px 0;font-size:12px;color:var(--muted)">WebSocket connected</p>
            <form action="/ext/jock/logout" method="post" style="display:inline">
                <button type="submit" class="danger-button">Disconnect</button>
            </form>"""
        else:
            if community_url:
                local_url = self.g.get("LOCAL_URL", "http://localhost:9100")
                callback = urllib.parse.quote(f"{local_url}/ext/jock/callback")
                login_url = f"{community_url.rstrip('/')}/auth/jock-login?redirect_uri={callback}"
                login_section = f'<a class="button green" href="{login_url}" style="margin-top:8px;display:inline-block">Login with Discord</a>'
            else:
                login_section = '<p class="subtle" style="color:var(--warning)">Enter the SHOWER server URL above, then click Save, then Login.</p>'

        url_form = ""
        if logged_in:
            url_form = f"""
            <form action="/ext/jock/sync-settings" method="post" id="jock-url-form" style="margin-top:12px">
                <label style="display:block;margin-bottom:4px;font-size:13px;color:var(--muted)">SHOWER Server URL</label>
                <div style="display:flex;gap:8px">
                    <input type="text" name="community_url" value="{esc(community_url)}" placeholder="http://localhost:9200" style="flex:1;font-family:monospace" id="jock-url-input" disabled>
                    <button type="button" id="jock-change-btn" onclick="jockChange()" class="button blue" style="white-space:nowrap">Change</button>
                </div>
            </form>"""
        else:
            url_form = f"""
            <form action="/ext/jock/sync-settings" method="post" style="margin-top:12px">
                <label style="display:block;margin-bottom:4px;font-size:13px;color:var(--muted)">SHOWER Server URL</label>
                <div style="display:flex;gap:8px">
                    <input type="text" name="community_url" value="{esc(community_url)}" placeholder="http://localhost:9200" style="flex:1;font-family:monospace">
                    <button type="submit" class="button green" style="white-space:nowrap">Login with Discord</button>
                </div>
            </form>"""

        return f"""
        <section class="panel">
            <div class="section-heading" onclick="toggleSection(this)" style="cursor:pointer">
                <h2>JOCK Strap <span class="collapse-arrow" style="font-size:12px;margin-left:6px;color:var(--muted)">&#9654;</span></h2>
            </div>
            <div class="collapse-content" style="display:none">
                {url_form}
                <hr style="border:none;border-top:1px solid var(--line);margin:16px 0">
                {login_section}
                <hr style="border:none;border-top:1px solid var(--line);margin:16px 0">
                <form action="/ext/jock/sync-settings" method="post" style="margin-top:8px;display:flex;align-items:center;gap:12px">
                    <label class="checkbox-label" style="margin:0">
                        <input type="checkbox" name="auto_sync" value="1" {auto_checked}>
                        Auto-sync inventory changes to community
                    </label>
                    <button type="submit" style="white-space:nowrap">Save Sync Settings</button>
                </form>
                <div style="margin-top:12px;display:flex;gap:8px">
                    <a class="button ghost" href="/ext/jock/sync-log">Sync Log</a>
                </div>
            </div>
        </section>
        <script>
        var JOCK_ORIG_URL = '{esc(community_url)}';
        function jockChange() {{
            var input = document.getElementById('jock-url-input');
            var btn = document.getElementById('jock-change-btn');
            if (btn.textContent === 'Change') {{
                input.disabled = false;
                input.focus();
                btn.textContent = 'Connect';
                btn.className = 'button green';
                var div = btn.parentElement;
                var cancel = document.createElement('button');
                cancel.type = 'button';
                cancel.textContent = 'Cancel';
                cancel.className = 'button ghost';
                cancel.onclick = function() {{
                    input.disabled = true;
                    input.value = JOCK_ORIG_URL;
                    btn.textContent = 'Change';
                    btn.className = 'button blue';
                    cancel.remove();
                }};
                div.appendChild(cancel);
            }} else {{
                var form = input.closest('form');
                form.submit();
            }}
        }}
        function toggleSection(el) {{
            var content = el.parentElement.querySelector('.collapse-content');
            var arrow = el.querySelector('.collapse-arrow');
            if (content.style.display === 'none') {{
                content.style.display = 'block';
                arrow.innerHTML = '&#9660;';
            }} else {{
                content.style.display = 'none';
                arrow.innerHTML = '&#9654;';
            }}
        }}
        </script>
        """

    def on_route(self, path, qs, data, method):
        if not path.startswith("/ext/jock/"):
            return None, False
        handlers = {
            "/ext/jock/callback": self._handle_callback,
            "/ext/jock/logout": self._handle_logout,
            "/ext/jock/sync": self._handle_sync,
            "/ext/jock/sync-settings": self._handle_sync_settings,
            "/ext/jock/sync-log": self._handle_sync_log,
            "/ext/jock/notifications": self._handle_notifications,
            "/ext/jock/notifications/read": self._handle_notification_read,
            "/ext/jock/orders": self._handle_orders,
            "/ext/jock/orders/create": self._handle_order_create,
            "/ext/jock/orders/fulfill": self._handle_order_fulfill,
            "/ext/jock/orders/mine": self._handle_my_orders,
            "/ext/jock/push-inventory": self._handle_push_inventory,
        }
        handler = handlers.get(path)
        if not handler:
            return None, False
        return handler(qs, data, method)

    def on_inventory_add(self, db, inv_id, data):
        self._auto_sync_inventory(db, "add", data)

    def on_inventory_update(self, db, inv_id, data):
        self._auto_sync_inventory(db, "update", data)

    def on_inventory_delete(self, db, inv_id, item_data=None):
        self._auto_sync_inventory(db, "delete", item_data or {"inv_id": str(inv_id)})

    def _auto_sync_inventory(self, db, action, data):
        if not self.sync_settings.get("auto_sync", False):
            return
        community_url = self.sync_settings.get("community_url", "")
        if not community_url:
            return
        itemid = data.get("itemid") or data.get("item_id", "")
        stationid = data.get("stationid") or data.get("station_id", "")
        item_name = ""
        station_name = ""
        if itemid:
            row = db.execute("SELECT name FROM item WHERE id=?", (int(itemid),)).fetchone()
            item_name = row["name"] if row else ""
        if stationid:
            row = db.execute("SELECT name FROM stations WHERE id=?", (int(stationid),)).fetchone()
            station_name = row["name"] if row else ""
        quality = data.get("qual", "")
        if action == "delete":
            quantity_scu = float(data.get("qty", 0)) / 100
        elif data.get("qty_scu"):
            quantity_scu = data.get("qty_scu", "")
        else:
            quantity_scu = float(data.get("qty", 0)) / 100
        ws_msg = {"type": "sync_inventory", "action": action,
                  "itemid": itemid, "item_name": item_name,
                  "quality": quality,
                  "quantity_scu": quantity_scu,
                  "stationid": stationid, "station": station_name}
        if self._ws_send(ws_msg):
            return
        # HTTP fallback with names
        try:
            body_data = {"item_name": item_name, "quality": quality,
                         "quantity_scu": quantity_scu, "station": station_name}
            if action == "delete":
                community_api("DELETE", "inventory/sync", community_url, token=token, body=body_data)
            else:
                community_api("POST", "inventory/sync", community_url, token=token, body=body_data)
        except Exception:
            pass

    # --- OAuth ---
    def _handle_callback(self, qs, data, method):
        code = qs.get("code", "")
        if not code:
            return self._redirect("/settings", "No auth code received from SHOWER.", "error")
        self._ws_connect(auth_code=code)
        import time
        for _ in range(50):
            if self._is_connected():
                return self._redirect("/settings", "Connected to SHOWER!")
            time.sleep(0.1)
        return self._redirect("/settings", "Connected to SHOWER but WebSocket connection failed. Check that the SHOWER server is reachable.", "error")

    # --- Logout ---
    def _handle_logout(self, qs, data, method):
        if method != "POST":
            return None, False
        self._ws_close()
        return self._redirect("/settings", "Disconnected.")

    # --- Sync ---
    def _handle_sync(self, qs, data, method):
        if method != "POST":
            return None, False
        if not self._is_connected():
            return self._redirect("/settings", "Not connected to SHOWER. Login with Discord first.", "error")
        return self._redirect("/settings", "Sync will happen automatically via WebSocket.")

    def _handle_sync_settings(self, qs, data, method):
        if method != "POST":
            return None, False
        community_url = data.get("community_url", "").strip()
        if not community_url:
            return self._redirect("/settings", "Enter a SHOWER server URL.", "error")
        self._ws_close()
        self.sync_settings["community_url"] = community_url
        if "auto_sync" in data:
            self.sync_settings["auto_sync"] = data.get("auto_sync") == "1"
        save_sync_settings(self.sync_settings)
        local_url = self.g.get("LOCAL_URL", "http://localhost:9100")
        callback = urllib.parse.quote(f"{local_url}/ext/jock/callback")
        login_url = f"{community_url.rstrip('/')}/auth/jock-login?redirect_uri={callback}"
        return self._redirect(login_url, "Redirecting to Discord login...")

    def _handle_sync_log(self, qs, data, method):
        rows_html = ""
        cache = get_data_dir() / "notifications_cache.json"
        logs = []
        if cache.exists():
            try:
                logs = json.loads(cache.read_text())
            except Exception:
                pass
        if not logs:
            logs = [{"direction": "info", "status": "ok", "message": "No sync activity yet.", "synced_at": ""}]
        for entry in logs[:50]:
            status_cls = "ok" if entry.get("status") == "ok" else "error"
            rows_html += f"<tr><td>{esc(entry.get('direction',''))}</td><td><span class='pill {status_cls}'>{esc(entry.get('status',''))}</span></td><td>{esc(entry.get('message',''))}</td><td>{esc(entry.get('created_at',''))}</td></tr>"
        content = f"""<section class="panel">
        <div class="section-heading"><h2>Sync Log</h2><a class="button ghost" href="/settings">Back</a></div>
        <div class="table-wrap"><table><thead><tr><th>Direction</th><th>Status</th><th>Message</th><th>Time</th></tr></thead><tbody>{rows_html}</tbody></table></div>
        </section>"""
        body = self._render_page(content)
        return body, True

    # --- Notifications ---
    def _handle_notifications(self, qs, data, method):
        notifs = self._cached_notifs()
        if not notifs:
            notifs = [{"title": "No notifications", "body": "You have no notifications yet.", "read": 1, "created_at": ""}]
        items = ""
        for n in notifs[:50]:
            cls = "" if n.get("read") else "unread"
            items += f"""<div class="notif {cls}">
            <div class="notif-body"><div class="notif-msg"><strong>{esc(n.get('title',''))}</strong> {esc(n.get('body',''))}</div>
            <div class="notif-time">{esc(n.get('created_at',''))}</div></div>
            <div class="notif-actions">{"<form action='/ext/jock/notifications/read' method='post' style='display:inline'><input type='hidden' name='notif_id' value=''><button class='notif-btn'>Mark Read</button></form>" if not n.get("read") else ""}</div></div>"""
        content = f"""<section class="panel">
        <div class="section-heading"><h2>Notifications</h2><a class="button ghost" href="/settings">Back</a></div>
        <div class="notif-list">{items}</div>
        </section>"""
        body = self._render_page(content)
        return body, True

    def _handle_notification_read(self, qs, data, method):
        if method != "POST":
            return None, False
        return self._redirect("/ext/jock/notifications", "Marked as read.")

    # --- Orders ---
    def _handle_orders(self, qs, data, method):
        community_url = self.sync_settings.get("community_url", "")
        token = self._get_token()
        orders, err = [], None
        if community_url and token:
            orders, err = community_api("GET", "orders?status=open", community_url, token=token)
        if err or not orders:
            orders = []
        rows = ""
        for o in orders[:50]:
            rows += f"<tr><td>{esc(o.get('item_name',''))}</td><td>{esc(o.get('min_quality',''))}</td><td>{esc(o.get('quantity',''))}</td><td>{esc(o.get('created_by_discord',''))}</td><td><form action='/ext/jock/orders/fulfill' method='post' style='display:inline'><input type='hidden' name='order_id' value='{esc(o.get('id',''))}'><button type='submit' class='button blue'>I Have This</button></form></td></tr>"
        if not rows:
            rows = '<tr><td colspan="5" class="empty">No open order requests.</td></tr>'
        content = f"""<section class="panel">
        <div class="section-heading"><h2>Open Order Requests</h2>
        <div><a class="button ghost" href="/ext/jock/orders/create">Create Request</a> <a class="button ghost" href="/ext/jock/orders/mine">My Requests</a></div>
        </div>
        <div class="table-wrap"><table><thead><tr><th>Item</th><th>Min Qual</th><th>QTY</th><th>Requester</th><th>Action</th></tr></thead><tbody>{rows}</tbody></table></div>
        </section>"""
        body = self._render_page(content)
        return body, True

    def _handle_order_create(self, qs, data, method):
        community_url = self.sync_settings.get("community_url", "")
        token = self._get_token()
        if method == "POST" and data:
            item_name = data.get("item_name", "")
            min_quality = data.get("min_quality", "1")
            quantity = data.get("quantity", "1")
            notes = data.get("notes", "")
            if community_url and token:
                resp, err = community_api("POST", "orders", community_url, token=token, body={
                    "item_name": item_name,
                    "min_quality": int(min_quality), "quantity": int(quantity), "notes": notes,
                })
                if err:
                    return self._redirect("/ext/jock/orders/create", f"Failed: {err}", "error")
                return self._redirect("/ext/jock/orders", "Order request created.")
            return self._redirect("/ext/jock/orders/create", "Not connected to SHOWER.", "error")
        form = """<form method="post" action="/ext/jock/orders/create" class="inline-form" style="flex-direction:column;align-items:stretch">
        <input type="text" name="item_name" placeholder="Item name" required>
        <input type="number" name="min_quality" placeholder="Minimum quality" min="1" max="1000" value="1" required>
        <input type="number" name="quantity" placeholder="Quantity" min="1" value="1" required>
        <textarea name="notes" placeholder="Notes (optional)" style="min-height:60px;resize:vertical"></textarea>
        <button type="submit">Submit Request</button>
        </form>"""
        content = f"""<section class="panel">
        <div class="section-heading"><h2>Create Order Request</h2><a class="button ghost" href="/ext/jock/orders">Back</a></div>
        {form}
        </section>"""
        body = self._render_page(content)
        return body, True

    def _handle_order_fulfill(self, qs, data, method):
        if method != "POST":
            return None, False
        community_url = self.sync_settings.get("community_url", "")
        token = self._get_token()
        order_id = data.get("order_id", "")
        if community_url and token and order_id:
            resp, err = community_api("POST", "orders/fulfill", community_url, token=token, body={
                "order_id": order_id,
            })
            if err:
                return self._redirect("/ext/jock/orders", f"Failed: {err}", "error")
            return self._redirect("/ext/jock/orders", "Notification sent to requester!")
        return self._redirect("/ext/jock/orders", "Not connected.", "error")

    def _handle_my_orders(self, qs, data, method):
        community_url = self.sync_settings.get("community_url", "")
        token = self._get_token()
        orders, err = [], None
        if community_url and token:
            orders, err = community_api("GET", "orders?status=my", community_url, token=token)
        if err or not orders:
            orders = []
        rows = ""
        for o in orders:
            status_cls = "ok" if o.get("status") == "fulfilled" else "hold"
            rows += f"<tr><td>{esc(o.get('item_name',''))}</td><td>{esc(o.get('min_quality',''))}</td><td>{esc(o.get('quantity',''))}</td><td><span class='pill {status_cls}'>{esc(o.get('status',''))}</span></td><td>{esc(o.get('assigned_to_discord',''))}</td></tr>"
        if not rows:
            rows = '<tr><td colspan="5" class="empty">No requests yet.</td></tr>'
        content = f"""<section class="panel">
        <div class="section-heading"><h2>My Order Requests</h2><a class="button ghost" href="/ext/jock/orders">Back</a></div>
        <div class="table-wrap"><table><thead><tr><th>Item</th><th>Min Qual</th><th>QTY</th><th>Status</th><th>Fulfilled By</th></tr></thead><tbody>{rows}</tbody></table></div>
        </section>"""
        body = self._render_page(content)
        return body, True

    # --- Push Inventory (from SHOWER reverse sync) ---
    def _handle_push_inventory(self, qs, data, method):
        if method != "POST":
            return json.dumps({"status": "error", "error": "POST required"}), True
        token = data.get("token", "")
        auth_data = load_auth()
        if not token or token != auth_data.get("client_token", ""):
            return json.dumps({"status": "error", "error": "Invalid token"}), True
        action = data.get("action", "")
        item_name = data.get("item_name", "").strip()
        if not item_name:
            return json.dumps({"status": "error", "error": "Missing item_name"}), True
        quality = int(data.get("quality", 100))
        quantity_scu = float(data.get("quantity_scu", 0))
        station = data.get("station", "").strip()
        store = self.g["store"]
        db = store.connect()
        try:
            if action == "add":
                row = db.execute("SELECT id FROM item WHERE name=? ORDER BY id LIMIT 1", (item_name,)).fetchone()
                if row:
                    itemid = row[0]
                else:
                    store.add_item(db, item_name, None)
                    itemid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
                stationid = None
                if station:
                    row = db.execute("SELECT id FROM stations WHERE name=? ORDER BY id LIMIT 1", (station,)).fetchone()
                    if row:
                        stationid = row[0]
                    else:
                        store.add_station(db, station, station, None)
                        stationid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
                qty_val = int(round(quantity_scu * 100))
                store.add_inventory(db, itemid, quality, qty_val, stationid)
                return json.dumps({"status": "ok"}), True
            elif action == "delete":
                row = db.execute("SELECT id FROM item WHERE name=? ORDER BY id LIMIT 1", (item_name,)).fetchone()
                if not row:
                    return json.dumps({"status": "error", "error": "Item not found"}), True
                itemid = row[0]
                stationid = None
                if station:
                    row = db.execute("SELECT id FROM stations WHERE name=? ORDER BY id LIMIT 1", (station,)).fetchone()
                    if row:
                        stationid = row[0]
                qty_val = int(round(quantity_scu * 100))
                if stationid:
                    inv = db.execute(
                        "SELECT id FROM inventory WHERE itemid=? AND qual=? AND qty=? AND stationid=? ORDER BY id LIMIT 1",
                        (itemid, quality, qty_val, stationid)
                    ).fetchone()
                else:
                    inv = db.execute(
                        "SELECT id FROM inventory WHERE itemid=? AND qual=? AND qty=? AND stationid IS NULL ORDER BY id LIMIT 1",
                        (itemid, quality, qty_val)
                    ).fetchone()
                if inv:
                    store.delete_inventory(db, inv[0])
                    return json.dumps({"status": "ok"}), True
                else:
                    return json.dumps({"status": "error", "error": "No matching inventory found"}), True
            else:
                return json.dumps({"status": "error", "error": f"Unknown action: {action}"}), True
        except Exception as e:
            db.rollback()
            return json.dumps({"status": "error", "error": str(e)}), True
        finally:
            db.close()

    # --- helpers ---
    def _render_page(self, content):
        from render import wrap_page
        return wrap_page(content, local_url=self.g.get("LOCAL_URL", ""), network_url=self.g.get("NETWORK_URL", ""), ext_ctx=self.g.get("EXTENSION_CONTEXTS", {}))

    def _redirect(self, location, notice="", kind="success"):
        from urllib.parse import urlencode
        sep = "&" if "?" in location else "?"
        if notice:
            location += sep + urlencode({"notice": notice, "kind": kind})
        body = f"""<!doctype html><html><body>
        <script>window.location.href='{location}';</script>
        <a href="{location}">Redirect</a></body></html>"""
        return body, True
