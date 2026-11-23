"""Embedded RunPod console and per-Pod JupyterLab sessions."""
from http.client import HTTPSConnection
from http.cookies import SimpleCookie
import json
from pathlib import Path
import re
import threading
from time import monotonic
from urllib.parse import urlencode, urlsplit

import gi
from gi import _gi
gi.require_version("Gtk", "4.0")
gi.require_version("Soup", "3.0")
try:
    gi.require_version("WebKit", "6.0")
except ValueError:
    _gi.Repository.get_default().prepend_search_path(str(Path(__file__).with_name("typelibs")))
    gi.require_version("WebKit", "6.0")
from gi.repository import Gio, GLib, Gtk, Soup, WebKit

import gpu


def target():
    return gpu.target_settings().get("id", "")


def lab_origin(pod):
    return f"https://{pod}-8888.proxy.runpod.net"


def login_cookies(origin, password):
    """Use the Jupyter login form on the exact Pod origin; never follow redirects with a secret."""
    conn = HTTPSConnection(urlsplit(origin).netloc, timeout=12)
    path = "/login?next=%2Flab"
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        headers = response.headers.get_all("Set-Cookie") or []
        response.read(262144)
        if response.status in (404, 502, 503, 504):
            raise ConnectionError("JupyterLabの起動を待っています。")
        jar = SimpleCookie()
        for header in headers:
            jar.load(header)
        if response.status != 200 or "_xsrf" not in jar or not password:
            return []
        xsrf = jar["_xsrf"].value
        data = urlencode({"password": password, "_xsrf": xsrf})
        conn.request("POST", path, data, {"Content-Type": "application/x-www-form-urlencoded",
                     "Cookie": "_xsrf=" + xsrf, "Origin": origin, "Referer": origin + path})
        reply = conn.getresponse()
        if reply.status not in (302, 303):
            return []
        return headers + (reply.headers.get_all("Set-Cookie") or [])
    finally:
        conn.close()


def lab_state(pod, connected):
    state = gpu.rest(gpu.load_key(), f"/v2/pods/{pod}")
    if state.get("id") != pod:
        raise ValueError("対象Podを確認できませんでした。")
    status = state.get("status")
    if status != "RUNNING":
        return status, []
    if "8888/http" not in (state.get("ports") or []):
        return "NO_PORT", []
    if connected:
        return status, []
    env = state.get("env") or {}
    password = env.get("JUPYTER_PASSWORD") or env.get("JUPYTER_TOKEN") or ""
    return status, login_cookies(lab_origin(pod), password)


def console_paths(profile):
    slot = profile.get("slot")
    if not slot or profile.get("signed_out"):
        return ()
    # The first release used one console directory; keep that session until its first sign-out.
    relative = Path("web-console")
    if profile.get("scope"):
        if not re.fullmatch(r"[0-9a-f]{32}", slot):
            raise ValueError("ブラウザセッションを確認してください。")
        relative = Path("web-sessions") / slot
    return tuple(Path(base) / "runpod-desktop" / relative
                 for base in (GLib.get_user_data_dir(), GLib.get_user_cache_dir()))


def new_session(console):
    paths = console_paths(gpu.connection()) if console else ()
    if not paths:
        return WebKit.NetworkSession.new_ephemeral()
    folder, cache = paths
    for path in (folder, cache):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    session = WebKit.NetworkSession.new(str(folder), str(cache))
    session.get_cookie_manager().set_persistent_storage(str(folder / "cookies.sqlite"),
                                                        WebKit.CookiePersistentStorage.SQLITE)
    return session


class PodWeb(Gtk.Box):
    def __init__(self, console, navigate, on_state=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        self.console = console
        self.navigate = navigate
        self.on_state = on_state
        self.pod = None
        self.section_url = None
        self.view = self.session = None
        self.popups = set()
        self.serial = 0
        self.checking = self.closed = self.loaded = False
        self.next_check = 0.0
        self.bar = Gtk.Box(spacing=8, margin_start=12, margin_end=12)
        self.back = Gtk.Button(label="戻る")
        self.back.connect("clicked", lambda _: self.view and self.view.go_back())
        self.back.set_sensitive(False)
        self.bar.append(self.back)
        self.origin = Gtk.Label(xalign=0, hexpand=True)
        self.origin.add_css_class("caption")
        self.bar.append(self.origin)
        self.append(self.bar)
        self.pages = Gtk.Stack(vexpand=True, hexpand=True)
        empty = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                        valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
        self.message = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self.message.add_css_class("heading")
        empty.append(self.message)
        actions = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        for text, action in (("再接続", lambda: self.activate(True)),
                             ("Podを操作", lambda: navigate("pod")),
                             ("GPU一覧・予約", lambda: navigate("market"))):
            control = Gtk.Button(label=text)
            control.connect("clicked", lambda _, f=action: f())
            actions.append(control)
        empty.append(actions)
        self.pages.add_named(empty, "status")
        self.append(self.pages)

    def status(self, message):
        self.loaded = False
        self.message.set_text(message)
        self.pages.set_visible_child_name("status")

    def discard(self):
        self.serial += 1
        self.section_url = None
        self.checking = self.loaded = False
        for popup in list(self.popups):
            popup.destroy()
        self.popups.clear()
        if self.view:
            self.view.stop_loading()
            self.pages.remove(self.view)
        self.view = self.session = None
        self.back.set_sensitive(False)

    def shutdown(self):
        self.closed = True
        self.discard()

    def activate(self, manual=False, url=None):
        if self.closed:
            return
        try:
            pod = target()
        except (OSError, ValueError):
            self.status("対象Podの設定を読み込めません。")
            return
        if pod != self.pod:
            self.discard()
            self.pod = pod
            self.next_check = 0
            self.origin.set_text("console.runpod.io" if self.console else f"Pod  {pod}")
        if self.console:
            if url:
                self.section_url = url
                self.open_view(url, [])
            elif self.view is None or self.pages.get_visible_child_name() == "status":
                self.open_view(self.section_url or "https://console.runpod.io/pods" + (f"?id={pod}" if pod else ""), [])
            elif manual:
                self.view.reload()
            return
        if not pod:
            self.status("使うGPUを一覧から選んでください。起動後に作業画面を開きます。")
            return
        if self.checking or (not manual and monotonic() < self.next_check):
            return
        self.next_check = monotonic() + 60
        self.checking = True
        self.serial += 1
        serial = self.serial
        connected = self.loaded and not manual
        if not self.loaded:
            self.status("JupyterLabの接続先を確認中…")
        def work():
            try:
                status, cookies = lab_state(pod, connected)
            except Exception:
                status, cookies = "RETRY", []
            GLib.idle_add(self.checked, serial, status, cookies, connected)
        threading.Thread(target=work, daemon=True).start()

    def checked(self, serial, state, cookies, connected):
        if self.closed or self.serial != serial:
            return
        self.checking = False
        try:
            current = target()
        except (OSError, ValueError):
            self.status("対象Podの設定を読み込めません。")
            return
        if self.pod != current:
            self.discard()
            return
        if state == "RUNNING":
            if not connected:
                self.open_view(lab_origin(self.pod) + "/lab", cookies)
            if self.on_state:
                self.on_state(self.pod, state)
            return
        if state == "RETRY" and self.loaded:
            self.origin.set_text(f"Pod  {self.pod}　·　接続状態を再確認中")
            return
        if state in ("EXITED", "TERMINATED") and self.view:
            self.discard()
        message = {
            "EXITED": "Podは停止中です。起動すると、ここにJupyterLabが開きます。",
            "TERMINATED": "Podは削除されています。GPU一覧から次のPodを選べます。",
            "NO_PORT": "このPodにはJupyterLab用の8888番HTTPポートがありません。",
            "RETRY": "JupyterLabの接続を待っています。1分後に再確認します。",
        }.get(state, "Podの起動を待っています。準備が整うと自動で接続します。")
        self.status(message)
        if self.on_state:
            self.on_state(self.pod, state)

    def open_view(self, url, headers):
        if self.view is None:
            self.session = new_session(self.console)
            self.session.connect("download-started", self.download)
            self.view = WebKit.WebView(network_session=self.session)
            self.configure(self.view)
            self.view.connect("load-failed", self.failed)
            self.view.connect("web-process-terminated", self.terminated)
            self.view.connect("notify::uri", self.location)
            self.view.connect("load-changed", self.location)
            self.pages.add_named(self.view, "web")
        origin = GLib.Uri.parse(url, GLib.UriFlags.NONE)
        cookies = []
        for header in headers:
            item = Soup.Cookie.parse(header, origin)
            if item and item.get_domain() == origin.get_host():
                cookies.append(item)
        # Install the Pod's own login cookies in its isolated session before opening /lab.
        self.load_with_cookies(self.session, self.serial, url, cookies)

    def load_with_cookies(self, session, serial, url, cookies):
        if self.closed or serial != self.serial:
            return
        if cookies:
            session.get_cookie_manager().add_cookie(cookies.pop(0), None, self.cookie_added,
                                                     (session, serial, url, cookies))
            return
        self.loaded = True
        self.pages.set_visible_child_name("web")
        self.view.load_uri(url)

    def cookie_added(self, manager, result, data):
        try:
            manager.add_cookie_finish(result)
        except GLib.Error:
            if not self.closed and self.serial == data[1]:
                self.status("接続情報を設定できませんでした。再接続してください。")
            return
        self.load_with_cookies(*data)

    def configure(self, view):
        view.connect("create", self.popup)
        view.connect("decide-policy", self.policy)

    def policy(self, view, decision, kind):
        if kind in (WebKit.PolicyDecisionType.NAVIGATION_ACTION, WebKit.PolicyDecisionType.NEW_WINDOW_ACTION):
            uri = decision.get_navigation_action().get_request().get_uri() or ""
            if not (urlsplit(uri).scheme == "https" or uri == "about:blank" or uri.startswith("blob:https://")):
                decision.ignore()
                return True
        return False

    def location(self, view, *_):
        if self.view != view:
            return
        host = urlsplit(view.get_uri() or "").hostname
        if host:
            self.origin.set_text(host)
        self.back.set_sensitive(view.can_go_back())

    def failed(self, view, event, uri, error):
        if self.view != view or error.matches(WebKit.NetworkError.quark(), WebKit.NetworkError.CANCELLED):
            return False
        self.status("ページを開けませんでした。接続を確認して「再接続」を押してください。")
        return True

    def terminated(self, view, reason):
        if self.view == view:
            self.status("ウェブ画面が終了しました。「再接続」で開き直せます。")

    def popup(self, parent, action):
        view = WebKit.WebView(related_view=parent)
        self.configure(view)
        win = Gtk.Window(transient_for=self.get_root(), default_width=900, default_height=680)
        win.set_child(view)
        self.popups.add(win)
        view.connect("notify::uri", lambda v, _: win.set_title(urlsplit(v.get_uri() or "").hostname or "RunPod"))
        view.connect("ready-to-show", lambda *_: win.present())
        view.connect("close", lambda *_: win.close())
        win.connect("close-request", lambda w: self.popups.discard(w) or False)
        return view

    def download(self, session, download):
        def destination(item, filename):
            dialog = Gtk.FileDialog(title="このPCに保存", initial_name=Path(filename).name or "download")
            def selected(picker, result):
                try:
                    path = picker.save_finish(result).get_path()
                    if path:
                        item.set_allow_overwrite(True)
                        item.set_destination(path)
                        return
                except GLib.Error:
                    pass
                item.cancel()
            dialog.save(self.get_root(), None, selected)
            return True
        download.connect("decide-destination", destination)
