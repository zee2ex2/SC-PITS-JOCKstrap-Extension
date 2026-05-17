import json
import os
import threading
import urllib.parse
import urllib.request
from http import HTTPStatus
from pathlib import Path

from extensions import Extension

AUTH_FILE = None
SYNC_SETTINGS_FILE = None
DISCORD_API = "https://discord.com/api/v10"

CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET", "")
REDIRECT_PATH = "/ext/jock/callback"


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
    return {"auto_sync": True, "community_url": "", "api_key": ""}


def save_sync_settings(data):
    if SYNC_SETTINGS_FILE:
        SYNC_SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SYNC_SETTINGS_FILE.write_text(json.dumps(data, indent=2))


def discord_api_request(method, endpoint, token=None, body=None):
    url = f"{DISCORD_API}/{endpoint}"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = body.encode() if isinstance(body, str) else (json.dumps(body).encode() if body else None)
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode()), None
    except urllib.error.HTTPError as e:
        detail = e.read().decode() if e.fp else ""
        return None, f"HTTP {e.code}: {detail}"
    except Exception as e:
        return None, str(e)


def exchange_code(code, redirect_uri):
    data = urllib.parse.urlencode({
        "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
    }).encode()
    resp, err = discord_api_request("POST", "oauth2/token", body=data)
    return resp, err


def get_guild_member(guild_id, access_token):
    resp, err = discord_api_request("GET", f"users/@me/guilds/{guild_id}/member", token=access_token)
    return resp, err


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
    description = "JOCK Strap — Discord OAuth, guild/role verification, community sync, orders, notifications"

    def on_startup(self, g):
        self.g = g
        g["ext_jock_auth"] = load_auth()
        if not CLIENT_ID or not CLIENT_SECRET:
            print("[jock_strap] WARNING: DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET env vars required")
        self.sync_settings = load_sync_settings()
        self._sync_engine = None
        self._start_sync_engine()

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

    def _poll_notifications(self):
        community_url = self.sync_settings.get("community_url", "")
        api_key = self.sync_settings.get("api_key", "")
        if not community_url or not api_key:
            return
        notifs, err = community_api("GET", "notifications", community_url, token=api_key, timeout=8)
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
        auth = self.g.get("ext_jock_auth", {})
        logged_in = bool(auth.get("access_token"))
        unread = self._unread_count()
        badge = f'<span class="notif-badge">{unread}</span>' if unread > 0 else ""
        nav = f"""
        <a class="button ghost" href="/ext/jock/orders">Orders</a>
        <a class="button ghost" href="/ext/jock/notifications">Notifs{badge}</a>
        """ if logged_in else ""
        return {
            "ext_jock_logged_in": str(logged_in).lower(),
            "ext_jock_tag": auth.get("discord_tag", ""),
            "ext_jock_guild_verified": str(auth.get("guild_verified", False)).lower(),
            "ext_jock_roles": auth.get("guild_roles", ""),
            "ext_jock_unread": str(unread),
            "ext_jock_community_url": self.sync_settings.get("community_url", ""),
            "ext_jock_has_api_key": str(bool(self.sync_settings.get("api_key", ""))).lower(),
            "_nav_html": nav,
        }

    def get_settings_html(self):
        auth = self.g.get("ext_jock_auth", {})
        logged_in = bool(auth.get("access_token"))
        tag = auth.get("discord_tag", "")
        roles = auth.get("guild_roles", "")
        verified = auth.get("guild_verified", False)
        selected_guild_name = auth.get("guild_name", "")
        community_url = self.sync_settings.get("community_url", "")
        api_key = self.sync_settings.get("api_key", "")
        auto_sync = self.sync_settings.get("auto_sync", True)
        auto_checked = 'checked' if auto_sync else ''

        status_color = "var(--accent)" if verified else "var(--danger)"
        status_text = "Verified" if verified else "Not Verified"
        guild_info = ""
        if selected_guild_name:
            guild_info = f"<p style='margin:4px 0'>Guild: <strong>{esc(selected_guild_name)}</strong> <span style='color:{status_color}'>({status_text})</span></p>"
        else:
            guild_info = f"<p style='margin:4px 0'>Guild: <span style='color:{status_color}'>Not selected</span></p>"

        login_section = ""
        if logged_in:
            guild_selector = ""
            guilds = auth.get("guilds", [])
            if guilds:
                opts = "".join(
                    f'<option value="{g["id"]}" {"selected" if g["id"] == auth.get("guild_id", "") else ""}>{esc(g.get("name", "?"))}</option>'
                    for g in guilds
                )
                guild_selector = f"""<form action="/ext/jock/guild-choose" method="post" style="margin-top:8px">
                <label style="display:block;margin-bottom:4px;font-size:13px;color:var(--muted)">Active Guild</label>
                <div style="display:flex;gap:8px"><select name="guild_id" style="flex:1">{opts}</select>
                <button type="submit">Switch</button></div>
                </form>"""
            login_section = f"""
            <p style="margin:12px 0">Logged in as <strong>{esc(tag)}</strong></p>
            {guild_info}
            {guild_selector}
            <p style="margin:4px 0">Roles: {esc(roles)}</p>
            <form action="/ext/jock/logout" method="post" style="display:inline">
                <button type="submit" class="danger-button">Disconnect Discord</button>
            </form>
            <form action="/ext/jock/sync" method="post" style="display:inline;margin-left:8px">
                <button type="submit">Sync Now</button>
            </form>
            """
        else:
            if CLIENT_ID:
                redirect = self.g.get("LOCAL_URL", "http://localhost:9100")
                encoded = urllib.parse.quote(f"{redirect}{REDIRECT_PATH}")
                url = f"https://discord.com/api/oauth2/authorize?client_id={CLIENT_ID}&response_type=code&redirect_uri={encoded}&scope=identify+guilds+guilds.members.read"
                login_section = f'<a class="button" href="{url}" style="margin-top:8px;display:inline-block">Login with Discord</a>'
            else:
                login_section = '<p class="subtle" style="color:var(--error)">DISCORD_CLIENT_ID not configured. Set the environment variable and restart PITS.</p>'

        return f"""
        <section class="panel">
            <div class="section-heading"><h2>JOCK Strap</h2></div>
            <p class="muted" style="font-size:13px">Discord OAuth login reads <code>DISCORD_CLIENT_ID</code> and <code>DISCORD_CLIENT_SECRET</code> from environment variables. Set them in your PITS environment and restart.</p>
            <hr style="border:none;border-top:1px solid var(--line);margin:16px 0">
            {login_section}
        </section>
        <section class="panel">
            <div class="section-heading"><h2>Community Sync</h2></div>
            <p>Sync your local inventory with the community database.</p>
            <form action="/ext/jock/sync-settings" method="post" style="margin-top:8px">
                <label class="checkbox-label">
                    <input type="checkbox" name="auto_sync" value="1" {auto_checked}>
                    Auto-sync changes to community
                </label>
                <div style="margin-top:12px">
                    <label style="display:block;margin-bottom:4px;font-size:13px;color:var(--muted)">Community API URL</label>
                    <input type="text" name="community_url" value="{esc(community_url)}" placeholder="http://localhost:9200" style="width:100%;font-family:monospace">
                </div>
                <div style="margin-top:12px">
                    <label style="display:block;margin-bottom:4px;font-size:13px;color:var(--muted)">API Key</label>
                    <input type="password" name="api_key" value="{esc(api_key)}" placeholder="Paste API key from SHOWER dashboard" style="width:100%;font-family:monospace">
                    <p class="subtle" style="margin-top:4px;font-size:12px;color:var(--muted)">Generate this from your SHOWER dashboard (Settings → API Keys). Replaces Discord auth for API calls.</p>
                </div>
                <button type="submit" style="margin-top:8px">Save Sync Settings</button>
            </form>
            <div style="margin-top:12px;display:flex;gap:8px">
                <a class="button ghost" href="/ext/jock/sync-log">Sync Log</a>
            </div>
        </section>
        """

    def on_route(self, path, qs, data, method):
        if not path.startswith("/ext/jock/"):
            return None, False
        handlers = {
            "/ext/jock/callback": self._handle_callback,
            "/ext/jock/guild-select": self._handle_guild_select,
            "/ext/jock/guild-choose": self._handle_guild_choose,
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
        }
        handler = handlers.get(path)
        if not handler:
            return None, False
        return handler(qs, data, method)

    def on_inventory_add(self, db, inv_id, data):
        self._auto_sync_inventory(db, "add", data)

    def on_inventory_update(self, db, inv_id, data):
        self._auto_sync_inventory(db, "update", data)

    def on_inventory_delete(self, db, inv_id):
        self._auto_sync_inventory(db, "delete", {"inv_id": str(inv_id)})

    def _auto_sync_inventory(self, db, action, data):
        if not self.sync_settings.get("auto_sync", False):
            return
        community_url = self.sync_settings.get("community_url", "")
        api_key = self.sync_settings.get("api_key", "")
        if not community_url or not api_key:
            return
        try:
            if action == "delete":
                community_api("DELETE", "inventory/sync", community_url, token=api_key, body={
                    "inventory_id": data.get("inv_id", ""),
                })
            else:
                community_api("POST", "inventory/sync", community_url, token=api_key, body={
                    "item_name": data.get("item_name", ""),
                    "quality": data.get("qual", ""),
                    "quantity_scu": data.get("qty_scu", ""),
                    "station": data.get("station_name", ""),
                })
        except Exception:
            pass

    # --- Logout ---
    def _handle_logout(self, qs, data, method):
        if method != "POST":
            return None, False
        save_auth({})
        self.g["ext_jock_auth"] = {}
        return self._redirect("/settings", "Disconnected from Discord.")

    # --- OAuth ---
    def _handle_callback(self, qs, data, method):
        code = qs.get("code", "")
        error = qs.get("error", "")
        if error or not code:
            return self._redirect("/settings", f"Discord auth error: {error}", "error")
        if not CLIENT_ID or not CLIENT_SECRET:
            return self._redirect("/settings", "Discord credentials not configured (env vars).", "error")
        local_url = self.g.get("LOCAL_URL", "http://localhost:9100")
        redirect_uri = f"{local_url}{REDIRECT_PATH}"
        token_data, err = exchange_code(code, redirect_uri)
        if err or not token_data:
            return self._redirect("/settings", f"Token exchange failed: {err}", "error")
        access_token = token_data.get("access_token")
        user_data, err = discord_api_request("GET", "users/@me", token=access_token)
        if err or not user_data:
            return self._redirect("/settings", f"Failed to get user: {err}", "error")
        discord_id = user_data.get("id")
        discord_tag = f"{user_data.get('username')}#{user_data.get('discriminator', '0')}"
        guilds_data, g_err = discord_api_request("GET", "users/@me/guilds", token=access_token)
        guilds = guilds_data if isinstance(guilds_data, list) else []
        auth = {
            "discord_id": discord_id, "discord_tag": discord_tag,
            "access_token": access_token,
            "refresh_token": token_data.get("refresh_token", ""),
            "expires_at": token_data.get("expires_in", 0),
            "guilds": guilds,
            "guild_id": "",
            "guild_name": "",
            "guild_verified": False,
            "guild_roles": "",
            "role_match": False,
        }
        save_auth(auth)
        self.g["ext_jock_auth"] = auth
        if guilds:
            return self._redirect("/ext/jock/guild-select", "Select your guild.")
        return self._redirect("/settings", "Logged in (no guilds found).")

    def _handle_guild_select(self, qs, data, method):
        auth = self.g.get("ext_jock_auth", {})
        guilds = auth.get("guilds", [])
        if not guilds:
            return self._redirect("/settings", "No guilds available.")
        opts = "".join(
            f'<option value="{g["id"]}">{esc(g.get("name", "?"))}</option>'
            for g in guilds
        )
        content = f"""<section class="panel">
        <div class="section-heading"><h2>Select Guild</h2></div>
        <p>Choose the Discord server (guild) to use for role verification.</p>
        <form action="/ext/jock/guild-choose" method="post" style="margin-top:12px">
            <label style="display:block;margin-bottom:4px;font-size:13px;color:var(--muted)">Guild</label>
            <select name="guild_id" style="width:100%;max-width:400px">{opts}</select>
            <button type="submit" style="margin-top:8px">Confirm</button>
        </form>
        <a class="button ghost" href="/settings" style="margin-top:8px;display:inline-block">Skip</a>
        </section>"""
        body = self._render_page(content)
        return body, True

    def _handle_guild_choose(self, qs, data, method):
        if method != "POST":
            return None, False
        auth = self.g.get("ext_jock_auth", {})
        guild_id = data.get("guild_id", "")
        access_token = auth.get("access_token", "")
        if not guild_id or not access_token:
            return self._redirect("/settings", "Missing guild or auth.", "error")
        guilds = auth.get("guilds", [])
        guild_name = next((g.get("name", "") for g in guilds if g["id"] == guild_id), "")
        member_data, m_err = get_guild_member(guild_id, access_token)
        guild_verified = bool(member_data and not m_err)
        guild_roles = ", ".join(member_data.get("roles", [])) if member_data else ""
        auth["guild_id"] = guild_id
        auth["guild_name"] = guild_name
        auth["guild_verified"] = guild_verified
        auth["guild_roles"] = guild_roles
        auth["role_match"] = guild_verified
        save_auth(auth)
        self.g["ext_jock_auth"] = auth
        status = f"Guild '{guild_name}' selected." if guild_verified else f"Guild '{guild_name}' selected but membership not confirmed."
        return self._redirect("/settings", status)

    # --- Sync ---
    def _handle_sync(self, qs, data, method):
        if method != "POST":
            return None, False
        community_url = self.sync_settings.get("community_url", "")
        api_key = self.sync_settings.get("api_key", "")
        if not community_url:
            return self._redirect("/settings", "Community URL not set.", "error")
        if not api_key:
            return self._redirect("/settings", "API key not set.", "error")
        resp, err = community_api("GET", "inventory/sync", community_url, token=api_key)
        if err:
            return self._redirect("/settings", f"Sync failed: {err}", "error")
        return self._redirect("/settings", "Sync complete.")

    def _handle_sync_settings(self, qs, data, method):
        if method != "POST":
            return None, False
        self.sync_settings["auto_sync"] = data.get("auto_sync") == "1"
        self.sync_settings["community_url"] = data.get("community_url", "").strip()
        new_key = data.get("api_key", "").strip()
        if new_key:
            self.sync_settings["api_key"] = new_key
        save_sync_settings(self.sync_settings)
        return self._redirect("/settings", "Sync settings saved.")

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
            rows_html += f"<tr><td>{esc(entry.get('direction',''))}</td><td><span class='pill {status_cls}'>{esc(entry.get('status',''))}</span></td><td>{esc(entry.get('message',''))}</td><td>{esc(entry.get('synced_at',''))}</td></tr>"
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
        api_key = self.sync_settings.get("api_key", "")
        orders, err = [], None
        if community_url and api_key:
            orders, err = community_api("GET", "orders?status=open", community_url, token=api_key)
        if err or not orders:
            orders = []
        rows = ""
        for o in orders[:50]:
            rows += f"<tr><td>{esc(o.get('item_name',''))}</td><td>{esc(o.get('min_quality',''))}</td><td>{esc(o.get('quantity',''))}</td><td>{esc(o.get('created_by_discord',''))}</td><td><form action='/ext/jock/orders/fulfill' method='post' style='display:inline'><input type='hidden' name='order_id' value='{esc(o.get('id',''))}'><button type='submit'>I Have This</button></form></td></tr>"
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
        api_key = self.sync_settings.get("api_key", "")
        if method == "POST" and data:
            item_name = data.get("item_name", "")
            min_quality = data.get("min_quality", "1")
            quantity = data.get("quantity", "1")
            notes = data.get("notes", "")
            if community_url and api_key:
                resp, err = community_api("POST", "orders", community_url, token=api_key, body={
                    "item_name": item_name,
                    "min_quality": int(min_quality), "quantity": int(quantity), "notes": notes,
                })
                if err:
                    return self._redirect("/ext/jock/orders/create", f"Failed: {err}", "error")
                return self._redirect("/ext/jock/orders", "Order request created.")
            return self._redirect("/ext/jock/orders/create", "Community not connected (set API key).", "error")
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
        api_key = self.sync_settings.get("api_key", "")
        order_id = data.get("order_id", "")
        if community_url and api_key and order_id:
            resp, err = community_api("POST", "orders/fulfill", community_url, token=api_key, body={
                "order_id": order_id,
            })
            if err:
                return self._redirect("/ext/jock/orders", f"Failed: {err}", "error")
            return self._redirect("/ext/jock/orders", "Notification sent to requester!")
        return self._redirect("/ext/jock/orders", "Not connected.", "error")

    def _handle_my_orders(self, qs, data, method):
        community_url = self.sync_settings.get("community_url", "")
        api_key = self.sync_settings.get("api_key", "")
        orders, err = [], None
        if community_url and api_key:
            orders, err = community_api("GET", "orders?status=my", community_url, token=api_key)
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

    # --- helpers ---
    def _render_page(self, content):
        from render import wrap_page
        return wrap_page(content, local_url=self.g.get("LOCAL_URL", ""), network_url=self.g.get("NETWORK_URL", ""))

    def _redirect(self, location, notice="", kind="success"):
        from urllib.parse import urlencode
        sep = "&" if "?" in location else "?"
        if notice:
            location += sep + urlencode({"notice": notice, "kind": kind})
        body = f"""<!doctype html><html><body>
        <script>window.location.href='{location}';</script>
        <a href="{location}">Redirect</a></body></html>"""
        return body, True
