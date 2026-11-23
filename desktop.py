#!/usr/bin/python3
import codecs
import datetime
from pathlib import Path
import subprocess
import sys
import threading
from time import monotonic

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk
import gpu
import account
import brand
import guide
import market
import pod_web
import strata_page
import rental

CSS = """
window.gpu { background: #f2f5f2; color: #172f2a; }
.gpu headerbar { background: #f2f5f2; color: #213d33; box-shadow: none; }
.markettitle { font-size: 25px; font-weight: bold; }
.stocktable, .stocklist, .stocklist row { background: #ffffff; color: #17392c; }
.stocktable { border: 1px solid #cedbd2; border-radius: 10px; }
.tablehead { background: #deebe1; color: #274b3b; font-weight: bold; border-radius: 9px 9px 0 0; padding: 8px 12px; }
.stockrow { padding: 5px 12px; border-bottom: 1px solid #e5ece7; }
.stockname { font-weight: bold; }
.stockyes { color: #097244; font-weight: bold; }
.stockno { color: #9c4541; }
.ordertray { padding: 9px 13px; border-radius: 9px; background: #e1ebe4; color: #254939; }
.hero { background: #153b33; color: #f3faf6; border-radius: 17px; padding: 18px 23px; }
.hero .subtitle { color: #b5d7c6; }
.gputitle { font-size: 29px; font-weight: 750; }
.caption { color: #526b61; font-size: 13px; }
.status { color: #c4efb4; font-weight: bold; }
.panel { background: #ffffff; color: #172f2a; border: 1px solid #d8e1d9; border-radius: 12px; padding: 12px 15px; }
.figure { color: #193b31; font-size: 23px; font-weight: 700; }
.heading { font-size: 16px; font-weight: bold; }
.gpu button { border-radius: 8px; }
.gpu button.action { padding: 8px 14px; background: #186849; color: white; font-weight: bold; }
.gpu button.action:hover { background: #125739; }
.gpu button:disabled { opacity: 0.50; }
.rental-guide { margin: 6px 18px 8px; padding: 10px 12px; background: #e5f1e8; color: #193b31; border-left: 3px solid #28764f; border-radius: 7px; }
.output { background: #e7ede8; border-radius: 8px; padding: 8px; }
.output textview, .output text { background: #e7ede8; color: #233c32; font-family: monospace; font-size: 12px; }
"""


def label(text, style=None, right=False):
    obj = Gtk.Label(label=text, xalign=1 if right else 0)
    if style:
        obj.add_css_class(style)
    return obj


def box(spacing=8, vertical=False, style=None):
    obj = Gtk.Box(orientation=Gtk.Orientation.VERTICAL if vertical else Gtk.Orientation.HORIZONTAL,
                  spacing=spacing)
    if style:
        obj.add_css_class(style)
    return obj


def button(text, callback, style=None):
    obj = Gtk.Button(label=text)
    if style:
        obj.add_css_class(style)
    obj.connect("clicked", lambda _: callback())
    return obj


def background(task, callback):
    def worker():
        try:
            value, error = task(), None
        except Exception as exc:
            value, error = None, str(exc)
        GLib.idle_add(callback, value, error)
    threading.Thread(target=worker, daemon=True).start()


STATE = {"RUNNING": "稼働中", "EXITED": "停止中", "STARTING": "起動中",
         "PROVISIONING": "準備中", "TERMINATED": "削除済み", "ERROR": "エラー"}


class Manager(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="RunPod GPU マネージャー")
        self.set_default_size(835, 635)
        self.add_css_class("gpu")
        self.state = {}
        self.key = None
        self.online = self.busy = self.polling = self.power_busy = False
        self.lock = threading.Lock()
        self.pending_log = ""
        bar = Gtk.HeaderBar()
        self.last_pod_check = float("-inf")
        self.follow_pod = self.follow_source = self.stopping_pod = None
        self.start_grace_until = 0
        self.page_title = label("RunPod GPU マネージャー", "heading")
        bar.set_title_widget(self.page_title)
        refresh = Gtk.Button(label="↻  更新", tooltip_text="表示中の画面を更新")
        refresh.connect("clicked", lambda _: self.refresh_page(manual=True))
        bar.pack_end(refresh)
        self.set_titlebar(bar)
        body = box(11, True)
        for side in ("bottom", "start", "end"):
            getattr(body, "set_margin_" + side)(18)
        body.set_margin_top(4)
        self.stack = Gtk.Stack()
        self.market = market.Market()
        self.account_ready = False
        self.console_return = None
        self.signing_out = False
        self.market.require_account = self.require_account
        self.stack.add_titled(self.market, "market", "GPU一覧・予約")
        self.stack.add_titled(body, "pod", "Podを操作")
        self.console_page = pod_web.PodWeb(True, self.stack.set_visible_child_name)
        self.lab_page = pod_web.PodWeb(False, self.stack.set_visible_child_name, self.lab_state_changed)
        self.stack.add_titled(self.console_page, "console", "RunPod管理")
        self.stack.add_titled(self.lab_page, "lab", "JupyterLab")
        self.strata = strata_page.StrataPage()
        self.stack.add_titled(self.strata, "strata", "本家Strata")
        self.account_page = account.Account(self.account_finished, self.open_credentials, self.request_sign_out,
            signup=lambda: self.open_console_section("https://console.runpod.io/signup", "account"))
        self.market.billing_btn.connect("clicked", lambda _: self.open_console_section("https://console.runpod.io/user/billing", "market"))
        self.market.account_btn.connect("clicked", lambda _: self.select_page(None, GLib.Variant("s", "account")))
        self.stack.add_named(self.account_page, "account")
        setup_action = Gio.SimpleAction.new("ai-setup", None)
        setup_action.connect("activate", self.ai_setup)
        self.add_action(setup_action)
        self.navigation = Gio.Menu()
        self.navigation.append("AIセットアップ", "win.ai-setup")
        for page, title in (("market", "GPU一覧・予約"), ("lab", "作業画面"), ("strata", "本家Strata · Qwen"),
                            ("pod", "Podの電源・状態"), ("console", "RunPod公式管理画面"),
                            ("account", "RunPodの接続設定")):
            self.navigation.append(title, "win.page::" + page)
        self.logout_action = Gio.SimpleAction.new("sign-out", None)
        self.logout_action.connect("activate", self.request_sign_out)
        self.logout_action.set_enabled(bool(gpu.connection().get("slot") or gpu.connection().get("pending_clear")))
        self.add_action(self.logout_action)
        self.navigation.append("サインアウト", "win.sign-out")
        self.guide = guide.RentalGuide(self)
        show_guide = Gio.SimpleAction.new("rental-guide", None)
        show_guide.connect("activate", self.guide.start)
        self.add_action(show_guide)
        self.navigation.append("初めてGPUを借りる", "win.rental-guide")
        about = Gio.SimpleAction.new("about", None)
        about.connect("activate", self.about)
        self.add_action(about)
        self.navigation.append("このソフトについて", "win.about")
        show_page = Gio.SimpleAction.new("page", GLib.VariantType.new("s"))
        show_page.connect("activate", self.select_page)
        self.add_action(show_page)
        bar.pack_start(Gtk.MenuButton(label="メニュー", menu_model=self.navigation))
        self.context_btn = button("電源・状態", self.context_action)
        self.context_btn.set_visible(False)
        bar.pack_end(self.context_btn)
        content = box(0, True)
        content.append(self.guide)
        self.stack.set_vexpand(True)
        content.append(self.stack)
        self.set_child(content)
        hero = box(12, style="hero")
        ident = box(3, True)
        ident.set_hexpand(True)
        ident.append(label("RunPod", "subtitle"))
        self.model = label(gpu.MODEL, "gputitle")
        ident.append(self.model)
        self.status = label("接続を確認中…", "status")
        ident.append(self.status)
        hero.append(ident)
        detail = box(5, True)
        detail.set_valign(Gtk.Align.CENTER)
        self.rate = label("GPU単価を確認中", "subtitle", True)
        detail.append(self.rate)
        self.pod_label = label("Pod  " + gpu.POD, "subtitle", True)
        detail.append(self.pod_label)
        hero.append(detail)
        body.append(hero)

        metrics = box(9)
        metrics.set_homogeneous(True)
        self.values = []
        for name, tip in (("GPU 使用率", "GPUコアの使用率"), ("空き GPU メモリ", "空き容量 / 総容量（GiB）"),
                          ("GPU 温度", "GPUコアの温度")):
            card = box(4, True, "panel")
            card.set_tooltip_text(tip)
            card.append(label(name, "caption"))
            value = label("—", "figure")
            card.append(value)
            self.values.append(value)
            metrics.append(card)
        body.append(metrics)

        actions = box(8)
        self.script_btn = button("Python ファイルを実行…", self.select_script, "action")
        self.test_btn = button("GPU テスト", self.test_gpu)
        self.shell_btn = button("SSH 端末", self.terminal)
        self.lab_btn = button("JupyterLabを開く", lambda: self.stack.set_visible_child_name("lab"))
        self.folder_btn = button("結果フォルダー", self.results)
        for item in (self.script_btn, self.test_btn, self.lab_btn, self.shell_btn, self.folder_btn):
            actions.append(item)
        body.append(actions)

        power = box(9)
        self.notice = label("", "caption")
        self.notice.set_wrap(True)
        self.notice.set_hexpand(True)
        power.append(self.notice)
        self.repair_btn = button("SSH 再接続", self.repair)
        self.start_btn = button("起動", lambda: self.confirm_power("start"))
        self.stop_btn = button("停止", lambda: self.confirm_power("stop"))
        for item in (self.repair_btn, self.start_btn, self.stop_btn):
            power.append(item)
        body.append(power)
        log_panel = box(6, True, "panel")
        log_panel.set_vexpand(True)
        log_header = box(6)
        title = label("実行ログ", "heading")
        title.set_hexpand(True)
        log_header.append(title)
        self.stamp = label("", "caption")
        log_header.append(self.stamp)
        log_panel.append(log_header)
        self.view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True,
                                 wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.buf = self.view.get_buffer()
        self.buf.set_text("Python ファイルを選ぶと、処理結果をこのPCに保存します。")
        scroll = Gtk.ScrolledWindow(min_content_height=80, vexpand=True)
        scroll.add_css_class("output")
        scroll.set_child(self.view)
        log_panel.append(scroll)
        body.append(log_panel)
        self.connect("close-request", self.on_close)
        self.sync_buttons()
        self.stack.connect("notify::visible-child-name", self.page_changed)
        self.market.connect("order-changed", lambda *_: self.follow_order())
        self.stack.connect("notify::visible-child-name", self.guide.refresh)
        self.market.connect("order-changed", self.guide.refresh)
        self.market.balance_label.connect("notify::label", self.guide.refresh)
        self.account_page.title.connect("notify::label", self.guide.refresh)
        self.poll_timer = GLib.timeout_add_seconds(12, self.poll_pod)
        self.log_timer = GLib.timeout_add(180, self.flush_log)
        if not gpu.connection().get("configured"):
            self.stack.set_visible_child_name("account")
        GLib.idle_add(self.check_connection)

    def about(self, *_):
        dialog = Gtk.AboutDialog(transient_for=self, modal=True, program_name=brand.PUBLIC_NAME,
                                 version=brand.VERSION, authors=[brand.AUTHOR], logo_icon_name="jp.local.RunpodDesktop")
        dialog.present()

    def request_sign_out(self, *_):
        if self.signing_out:
            return
        if self.busy or self.power_busy or self.market.changing or self.account_page.busy:
            Gtk.AlertDialog(message="操作の完了後にサインアウトできます。").show(self)
            return
        dialog = Gtk.AlertDialog(message="このPCのRunPodからサインアウトしますか？",
            detail="保存したAPIキーと管理画面のログイン情報をこのPCから消します。空き待ちの予約は取り消します。稼働中のPodはサインアウト後も課金されます。")
        dialog.set_buttons(["戻る", "サインアウト"])
        dialog.set_default_button(0)
        dialog.set_cancel_button(0)
        def decided(alert, result):
            try:
                if alert.choose_finish(result) == 1:
                    self.perform_sign_out()
            except GLib.Error:
                pass
        dialog.choose(self, None, decided)

    def perform_sign_out(self):
        if self.signing_out:
            return
        self.signing_out = True
        self.set_sensitive(False)
        self.strata.reset()
        self.console_page.discard()
        self.lab_page.discard()
        self.follow_pod = self.follow_source = None
        def finished(_, error):
            self.signing_out = False
            self.set_sensitive(True)
            if error and not gpu.connection().get("signed_out"):
                self.refresh_page()
                Gtk.AlertDialog(message="サインアウトできませんでした。", detail=error).show(self)
                return
            self.key = None
            self.account_ready = False
            app = self.get_application()
            fresh = Manager(app)
            fresh.present()
            self.close()
            if error:
                fresh.account_page.note.set_text("キーリングまたはブラウザデータの消去を再試行してください。")
        background(account.sign_out, finished)

    def check_connection(self):
        def check():
            if gpu.connection().get("pending_clear"):
                account.sign_out()
            key = gpu.load_key()
            if not key:
                return False
            if not gpu.connection().get("configured"):
                return True
            try:
                gpu.rest(key, "/v2/pods?limit=1")
            except gpu.ApiError as exc:
                if exc.status in (401, 403):
                    return False
            except OSError:
                pass
            return True
        def checked(ok, error):
            if self.market.closed or self.signing_out:
                return
            if error or not ok:
                self.stack.set_visible_child_name("account")
                if error:
                    self.account_page.note.set_text("このPCのキーリングを確認してください。")
            elif not gpu.connection().get("configured"):
                self.stack.set_visible_child_name("account")
                self.account_page.discover()
            else:
                self.account_finished()
        background(check, checked)

    def account_finished(self):
        self.logout_action.set_enabled(True)
        self.console_page.discard()
        self.account_ready = True
        if self.lab_page.pod != gpu.POD:
            self.lab_page.discard()
        self.key = None
        self.state = {}
        self.online = False
        self.model.set_text(gpu.MODEL)
        self.pod_label.set_text("Pod  " + gpu.POD if gpu.POD else "Pod未選択")
        self.sync_buttons()
        self.market.show_billing(not gpu.POD)
        self.stack.set_visible_child_name("market")
        self.guide.refresh()
        self.initial_page()

    def require_account(self):
        if not self.account_ready:
            self.stack.set_visible_child_name("account")
        return self.account_ready

    def open_credentials(self):
        self.open_console_section("https://console.runpod.io/user/settings", "account")

    def open_console_section(self, url, return_page):
        self.console_return = return_page
        self.stack.set_visible_child_name("console")
        self.console_page.activate(url=url)

    def initial_page(self):
        if self.market.closed:
            return
        order = self.market.order or {}
        if (order.get("state") == "ready" and order.get("pod") == pod_web.target()) or (
                order and order.get("state") not in rental.TERMINAL):
            self.follow_order()
        elif pod_web.target():
            self.begin_work(pod_web.target(), "startup")

    def follow_order(self):
        order = self.market.order or {}
        if order.get("pod") and self.market.first_gpu:
            self.market.show_billing(False)
        self.follow_pod = self.follow_source = None
        if order.get("state") == "ready" and order.get("pod") == pod_web.target():
            self.begin_work(order["pod"], order["name"])

    def begin_work(self, pod, source):
        if self.signing_out:
            return
        order = self.market.order or {}
        jupyter_only = order.get("pod") == pod and order.get("connection_mode") == "jupyter"
        if not jupyter_only and self.strata.auto.get_active() and rental.inventory.strata_candidate(gpu.target_settings()) is not False:
            self.follow_pod = self.follow_source = None
            self.stack.set_visible_child_name("strata")
            self.strata.activate()
            return
        self.follow_pod, self.follow_source = pod, source
        if source == "start":
            self.start_grace_until = monotonic() + 90
            self.lab_page.discard()
        self.lab_page.next_check = 0
        self.lab_page.activate()

    def lab_state_changed(self, pod, state):
        if pod != pod_web.target():
            return
        page = self.stack.get_visible_child_name()
        if state in ("EXITED", "TERMINATED"):
            if state == "EXITED" and self.follow_source == "start" and monotonic() < self.start_grace_until:
                return
            if page == "lab" or self.follow_source not in (None, "startup"):
                self.market.message.set_text("Podは停止中です。次に使うGPUを選べます。")
            self.follow_pod = self.follow_source = None
            if page == "lab":
                self.stack.set_visible_child_name("market")
            return
        if self.follow_pod != pod:
            return
        if state == "RUNNING":
            order = rental.read_order()
            if order and (order["state"] not in rental.TERMINAL or
                          (self.follow_source not in ("start", "startup") and
                           (order.get("name") != self.follow_source or order.get("state") != "ready"))):
                self.follow_pod = self.follow_source = None
                return
            self.follow_pod = self.follow_source = None
            self.market.message.set_text("GPUに接続しました。作業画面を開けます。")
            self.stack.set_visible_child_name("lab")
        elif state in ("ERROR", "NO_PORT") or (self.follow_source == "startup" and state == "RETRY"):
            if self.follow_source != "startup":
                self.market.message.set_text(self.lab_page.message.get_text())
            self.follow_pod = self.follow_source = None
        else:
            self.market.message.set_text(self.lab_page.message.get_text())

    def context_action(self):
        if self.stack.get_visible_child_name() == "console" and self.console_return:
            page = self.console_return
            self.stack.set_visible_child_name(page)
            if page == "market":
                self.market.check_balance()
            return
        if self.stack.get_visible_child_name() in ("lab", "strata"):
            self.stack.set_visible_child_name("pod")
        else:
            self.stack.set_visible_child_name("lab" if self.lab_page.loaded else "market")

    def ai_setup(self, *_):
        self.strata.setup()
        self.stack.set_visible_child_name("strata")

    def select_page(self, _, value):
        if self.follow_source == "startup":
            self.follow_pod = self.follow_source = None
        page = value.get_string()
        if page == "account" and (self.busy or self.power_busy):
            self.notice.set_text("実行中の操作が完了したら、接続設定を開けます。")
            self.stack.set_visible_child_name("pod")
            return
        if page == "account" and self.account_ready and not self.account_page.busy:
            self.account_page.discover(auto=False)
        if page in ("pod", "lab") and not self.require_account():
            return
        self.stack.set_visible_child_name(page)

    def page_changed(self, *_):
        page = self.stack.get_visible_child_name()
        if page != "console":
            self.console_return = None
            self.console_page.section_url = None
        titles = {"market": "RunPod GPU マネージャー", "lab": "作業画面 · JupyterLab",
                  "pod": "Podの電源・状態", "console": "RunPod公式管理画面", "account": "RunPodの接続設定", "strata": "本家Strata · Qwen"}
        self.page_title.set_text(titles[page])
        self.context_btn.set_visible(page != "market" or self.lab_page.loaded)
        self.context_btn.set_label("電源・状態" if page in ("lab", "strata") else
                                   ("作業に戻る" if self.lab_page.loaded else "GPU一覧に戻る"))
        if page == "console" and self.console_return:
            self.context_btn.set_label("残高を再確認して戻る" if self.console_return == "market" else "接続設定に戻る")
        self.refresh_page()

    def refresh_page(self, manual=False):
        page = self.stack.get_visible_child()
        if page in (self.console_page, self.lab_page):
            if page is self.console_page and self.console_return and not manual:
                return
            page.activate(manual)
        elif page == self.strata:
            self.strata.activate()
        elif page == self.market:
            if manual:
                self.market.fetch(manual=True)
            else:
                self.market.update_order()
        else:
            self.poll_pod()

    def poll_pod(self):
        if self.strata.pod and self.strata.pod != pod_web.target():
            self.strata.reset()
        if self.signing_out:
            return True
        if self.follow_pod or (self.stack.get_visible_child_name() == "lab" and market.on_screen(self)):
            self.lab_page.activate()
        if self.stack.get_visible_child_name() == "pod" and market.on_screen(self):
            if monotonic() - self.last_pod_check >= 5:
                self.refresh()
        return True

    def on_close(self, *_):
        if self.busy or self.power_busy or self.signing_out:
            self.notice.set_text("処理中です。完了までこのウィンドウを最小化できます。")
            return True
        self.strata.shutdown()
        self.market.shutdown()
        self.console_page.shutdown()
        self.lab_page.shutdown()
        GLib.source_remove(self.poll_timer)
        GLib.source_remove(self.log_timer)
        return False

    def sync_buttons(self):
        allowed = self.state.get("actions") or []
        idle = not self.power_busy and not self.busy
        for b in (self.test_btn, self.script_btn):
            b.set_sensitive(idle and self.online)
        proxy = not self.online and gpu.proxy_shell(self.state)
        self.shell_btn.set_sensitive(bool(idle and (self.online or proxy)))
        self.shell_btn.set_label("SSH 端末（RunPod経由）" if proxy else "SSH 端末")
        direct = (self.state.get("ssh") or {}).get("direct") or {}
        self.repair_btn.set_visible(bool(self.key and self.state.get("status") == "RUNNING" and not self.online
                                         and direct.get("host") and direct.get("port")))
        self.repair_btn.set_sensitive(idle)
        order = rental.read_order()
        self.start_btn.set_sensitive(idle and "start" in allowed and (not order or order.get("state") in rental.TERMINAL))
        self.stop_btn.set_sensitive(idle and "stop" in allowed)

    def refresh(self):
        if self.polling or self.power_busy:
            return True
        self.last_pod_check = monotonic()
        if not self.busy:
            previous = gpu.POD
            gpu.reload_target()
            if gpu.POD != previous:
                self.online = False
                self.state = {}
                self.sync_buttons()
                self.model.set_text(gpu.MODEL)
                self.pod_label.set_text("Pod  " + gpu.POD)
        self.polling = True
        def read():
            key = None
            state = {}
            issues = []
            try:
                key = gpu.load_key()
                if key and gpu.POD:
                    state = gpu.api(key)
                elif key:
                    issues.append("GPU一覧から使うGPUを選んでください。")
                else:
                    issues.append("電源操作には専用キーの登録が必要です。")
            except Exception as exc:
                issues.append(str(exc))
            data = None
            if state.get("status") == "RUNNING" or (not state and gpu.POD and gpu.POD == gpu.LEGACY_POD):
                try:
                    if state and gpu.address_changed(state):
                        raise RuntimeError("SSH接続先が更新されました。")
                    data = gpu.telemetry()
                except Exception as exc:
                    direct = (state.get("ssh") or {}).get("direct") or {}
                    if state and not (direct.get("host") and direct.get("port")):
                        issues.append("RunPodから直接SSHの公開アドレスとポートが割り当てられていません。")
                    else:
                        issues.append("SSHの接続を確認してください。")
                    if not key:
                        issues.append(str(exc)[:110])
            return key, state, data, issues
        background(read, self.show_state)
        return True

    def show_state(self, result, error):
        self.polling = False
        if self.power_busy or self.signing_out or self.market.closed:
            return
        if error:
            self.notice.set_text(error)
            return
        self.key, self.state, data, issues = result
        self.online = bool(data)
        key = self.state.get("status")
        if data:
            name, usage, used, total, temp = data
            self.model.set_text(name.removeprefix("NVIDIA "))
            numbers = (f"{usage} %", f"{(total-used)/1024:.1f} GiB", f"{temp} °C")
            self.values[1].set_tooltip_text(f"空き {total-used:,} MiB ／ 合計 {total:,} MiB")
        else:
            numbers = ("—", "—", "—")
        for metric, value in zip(self.values, numbers):
            metric.set_text(value)
        status = STATE.get(key, "電源状態不明")
        self.status.set_text(f"●  {status}　·　SSH 接続済み" if data else f"○  {status}")
        order = rental.read_order()
        if (not data and key == "RUNNING" and order.get("pod") == gpu.POD and
                order.get("state") == "ready" and order.get("connection_mode") == "jupyter"):
            self.status.set_text(f"●  {status}　·　JupyterでGPU計算確認済み")
        rate = self.state.get("cost")
        self.rate.set_text(f"GPUの時間単価　＄{rate:.2f} / 時間" if isinstance(rate, (int,float)) else "GPU単価　—")
        note = "ウィンドウを閉じてもPodは稼働を続けます。" if key == "RUNNING" else status
        self.notice.set_text("  ".join(issues) or note)
        self.stamp.set_text(datetime.datetime.now().strftime("%H:%M:%S 更新"))
        self.sync_buttons()
        if self.stopping_pod == gpu.POD and key in ("EXITED", "TERMINATED"):
            self.stopping_pod = None
            self.strata.reset()
            self.lab_page.discard()
            self.stack.set_visible_child_name("market")
            self.market.message.set_text("GPUを停止しました。次に使うGPUを選べます。")

    def queue_log(self, text):
        with self.lock:
            self.pending_log = (self.pending_log + text)[-32000:]

    def flush_log(self):
        with self.lock:
            text, self.pending_log = self.pending_log, ""
        if text:
            self.buf.insert(self.buf.get_end_iter(), text)
            count = self.buf.get_char_count()
            if count > 40000:
                self.buf.delete(self.buf.get_start_iter(), self.buf.get_iter_at_offset(count - 32000))
            self.view.scroll_to_iter(self.buf.get_end_iter(), 0, False, 0, 0)
        return True

    def perform(self, message, task):
        if self.busy or self.power_busy:
            return
        self.busy = True
        self.sync_buttons()
        self.buf.set_text(message + "\n")
        def done(value, error):
            self.flush_log()
            self.queue_log((error or value or "完了しました。") + "\n")
            self.flush_log()
            self.busy = False
            self.sync_buttons()
            self.refresh()
        background(task, done)

    def test_gpu(self):
        self.perform(f"{gpu.MODEL} で計算しています…", gpu.gpu_test)

    def select_script(self):
        fil = Gtk.FileFilter(name="Python ファイル")
        fil.add_pattern("*.py")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(fil)
        dialog = Gtk.FileDialog(title="Podで実行するPythonファイルを選択", filters=filters)
        def selected(dlg, result):
            try:
                item = dlg.open_finish(result)
            except GLib.Error:
                return
            if item and item.get_path():
                self.run_script(Path(item.get_path()))
        dialog.open(self, None, selected)

    def run_script(self, path):
        def task():
            cmd = [sys.executable, "-u", str(Path(__file__).with_name("gpu.py")), str(path)]
            with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  stdin=subprocess.DEVNULL) as proc:
                decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                while chunk := proc.stdout.read1(4096):
                    self.queue_log(decoder.decode(chunk))
                self.queue_log(decoder.decode(b"", final=True))
                code = proc.wait()
            return "実行完了" if code == 0 else f"終了コード {code}。実行ログを確認してください。"
        self.perform(f"{path.name} を実行しています…", task)

    def results(self):
        gpu.RESULTS.mkdir(parents=True, exist_ok=True)
        Gio.AppInfo.launch_default_for_uri(gpu.RESULTS.as_uri(), None)

    def terminal(self):
        if not self.online:
            command = gpu.proxy_shell(self.state)
            if command:
                subprocess.Popen(["ptyxis", "--new-window", "--", *command])
            return
        subprocess.Popen(["ptyxis", "--new-window", "--", "ssh", "-t", *gpu.ssh_options(), gpu.HOST,
                          "cd /workspace && exec bash -l"])

    def repair(self):
        self.busy = True
        self.sync_buttons()
        self.buf.set_text("現在のPod接続先を照合しています…")
        def finished(_, error):
            self.busy = False
            self.sync_buttons()
            if error:
                self.buf.set_text(error)
                self.recovery_window()
            else:
                self.buf.set_text("SSH接続を更新しました。")
                self.refresh()
        background(lambda: gpu.reconnect(self.key), finished)

    def recovery_window(self):
        win = Gtk.Window(title="PodのSSH接続を復元", transient_for=self, modal=True,
                         default_width=480)
        content = box(12, True, "panel")
        win.set_child(content)
        note = label("Podの再作成などでSSH鍵が変わった場合は、対象PodのJupyterLabで「Terminal」を開き、下のコマンドを貼り付けて実行してください。")
        note.set_wrap(True)
        content.append(note)
        links = box()
        def open_lab():
            win.close()
            self.stack.set_visible_child_name("lab")
        links.append(button("アプリ内でJupyterLabを開く", open_lab))
        links.append(button("復元コマンドをコピー", lambda: self.get_clipboard().set(gpu.recovery_command())))
        content.append(links)
        entry = Gtk.Entry(placeholder_text="表示された ssh-ed25519 で始まるホスト公開鍵")
        content.append(entry)
        msg = label("")
        msg.set_wrap(True)
        content.append(msg)
        def apply():
            value = entry.get_text().strip()
            if not value:
                msg.set_text("Podに表示されたホスト公開鍵を貼ってください。")
                return
            confirm.set_sensitive(False)
            def finished(_, error):
                confirm.set_sensitive(True)
                if error:
                    msg.set_text(error)
                else:
                    win.close()
                    self.buf.set_text("Podのホスト鍵を照合してSSH接続を更新しました。")
                    self.refresh()
            background(lambda: gpu.reconnect(self.key, value), finished)
        confirm = button("照合して再接続", apply, "action")
        content.append(confirm)
        win.present()

    def confirm_power(self, action):
        start = action == "start"
        name = "起動" if start else "停止"
        cost = self.state.get("cost")
        if start:
            desc = f"このPodのGPU計算料金（＄{cost:.2f} / 時間）が発生します。" if isinstance(cost,(int,float)) else "このPodのGPU計算料金が発生します。"
        else:
            desc = "実行中の処理が終了します。Podの一時ディスクにあるファイルは失われる場合があります。このPCに必要な結果を保存してから停止してください。"
        dialog = Gtk.AlertDialog(message=f"{gpu.MODEL} を{name}しますか？", detail=desc)
        dialog.set_buttons(["キャンセル", name])
        dialog.set_cancel_button(0)
        dialog.set_default_button(0)
        def chosen(dlg, result):
            try:
                if dlg.choose_finish(result) != 1:
                    return
            except GLib.Error:
                return
            if self.busy or self.power_busy:
                return
            self.power_busy = True
            self.sync_buttons()
            self.buf.set_text(f"RunPodに{name}を要求しています…")
            def finish(value, error):
                self.power_busy = False
                self.state = value or {}
                self.online = False
                self.sync_buttons()
                self.buf.set_text(error or f"{name}要求を受け付けました。Podの状態を更新しています。")
                if not error:
                    if start:
                        order = rental.read_order()
                        if order.get("pod") == gpu.POD and order.get("state") == "booting":
                            self.stack.set_visible_child_name("market")
                            self.market.set_order(order)
                        else:
                            self.begin_work(gpu.POD, "start")
                    else:
                        self.follow_pod = self.follow_source = None
                        self.stopping_pod = gpu.POD
                self.refresh()
            background(lambda: gpu.change_power(self.key, action), finish)
        dialog.choose(self, None, chosen)


class Desktop(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="jp.local.RunpodDesktop")
    def do_activate(self):
        window = self.get_active_window()
        if not window:
            css = Gtk.CssProvider()
            css.load_from_string(CSS)
            Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            window = Manager(self)
        window.present()


if __name__ == "__main__":
    sys.exit(Desktop().run(sys.argv))
