"""The official Strata chat, embedded in the GPU manager's existing window."""
import threading
from urllib.parse import urlsplit

import gpu
import inventory
import strata_bridge as bridge
import strata_setup
from pod_web import Gio, GLib, Gtk, WebKit


class StrataPage(Gtk.Box):
    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=7)
        self.pod = self.cancel = self.view = self.origin = None
        self.generation = 0
        self.closed = False
        bar = Gtk.Box(spacing=12, margin_start=14, margin_end=14)
        self.indicator = Gtk.Label(label='GPU未接続', xalign=0, hexpand=True)
        bar.append(self.indicator)
        self.auto = Gtk.CheckButton(label='VRAM 24 GB以上のNVIDIA GPUで自動接続')
        self.auto.set_active(bridge.OPTION.exists())
        self.auto.connect('toggled', self.preference)
        bar.append(self.auto)
        self.append(bar)
        controls = Gtk.Box(spacing=8, margin_start=14, margin_end=14)
        self.setup_btn = Gtk.Button(label='AIセットアップ')
        self.setup_btn.connect('clicked', lambda _: self.setup())
        controls.append(self.setup_btn)
        self.updates_btn = Gtk.Button(label='更新を確認')
        self.updates_btn.connect('clicked', lambda _: self.check_updates())
        controls.append(self.updates_btn)
        self.info = Gtk.Label(xalign=0, wrap=True, hexpand=True)
        controls.append(self.info)
        self.append(controls)
        self.pages = Gtk.Stack(vexpand=True, hexpand=True)
        waiting = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
        self.note = Gtk.Label(label='VRAM 24 GB以上のNVIDIA GPUを選んでください。', wrap=True, justify=Gtk.Justification.CENTER)
        self.note.add_css_class('heading')
        waiting.append(self.note)
        self.retry = Gtk.Button(label='再接続', halign=Gtk.Align.CENTER)
        self.retry.connect('clicked', lambda _: self.activate(retry=True))
        waiting.append(self.retry)
        self.pages.add_named(waiting,'status')
        self.append(self.pages)

    def setup(self):
        if not self.setup_btn.get_sensitive():
            return
        self.auto.set_active(True)
        if gpu.target_settings().get('id'):
            self.activate(retry=True)
            return
        self.setup_btn.set_sensitive(False)
        def message(value):
            GLib.idle_add(self.info.set_text, value)
        def work():
            try:
                strata_setup.setup_local(message)
                GLib.idle_add(done, True, '自宅SSDのQwen IQ3_Sを照合しました。')
            except Exception as exc:
                GLib.idle_add(done, False, f'AIセットアップ　{exc}')
        def done(ok, message):
            if self.closed:
                return False
            self.info.set_text(message)
            self.setup_btn.set_sensitive(True)
            if ok and (not self.cancel or self.retry.get_visible()):
                self.activate(retry=True)
            return False
        threading.Thread(target=work, daemon=True).start()

    def check_updates(self):
        if not self.updates_btn.get_sensitive():
            return
        self.updates_btn.set_sensitive(False)
        self.info.set_text('本家StrataとQwenの更新を確認中…')
        def work():
            try:
                message = strata_setup.check_updates()
            except Exception as exc:
                message = f'更新を確認できませんでした。{exc}'
            GLib.idle_add(done, message)
        def done(message):
            self.info.set_text(message)
            self.updates_btn.set_sensitive(True)
            return False
        threading.Thread(target=work, daemon=True).start()

    def working(self):
        """True while Strata is being prepared, which can take minutes without GPU use."""
        return not self.closed and not self.setup_btn.get_sensitive()

    def preference(self, button):
        if button.get_active():
            bridge.OPTION.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            bridge.OPTION.touch(mode=0o600)
        else:
            bridge.OPTION.unlink(missing_ok=True)

    def reset(self):
        self.generation += 1
        if self.cancel:
            self.cancel.set()
        self.cancel = self.pod = None
        self.pages.set_visible_child_name('status')
        if self.view:
            self.view.stop_loading()
            self.pages.remove(self.view)
        self.view = self.origin = None
        self.indicator.set_text('GPU未接続')
        self.note.set_text('GPUの接続を待っています。')
        self.setup_btn.set_sensitive(True)
        self.retry.set_visible(True)

    def shutdown(self):
        self.closed = True
        self.reset()

    def activate(self, retry=False):
        if self.closed:
            return
        target = gpu.target_settings()
        if self.cancel and target.get('id') != self.pod:
            self.reset()
        if not self.setup_btn.get_sensitive():
            return
        if target.get('id') != self.pod or retry:
            self.reset()
        if inventory.strata_candidate(target) is False:
            self.note.set_text('「GPU一覧・予約」でVRAM 24 GB以上のNVIDIA GPUを1枚選んでください。')
            return
        if self.cancel:
            return
        if not strata_setup.source_paths() and not self.auto.get_active():
            self.note.set_text('「AIセットアップ」を押すと、GPUを確認して自宅SSDにQwenを用意します。')
            self.retry.set_visible(False)
            return
        gpu.reload_target()
        self.pod = target['id']
        try:
            ssh = gpu.ssh()
        except ValueError as exc:
            self.note.set_text(str(exc))
            return
        self.retry.set_visible(False)
        self.note.set_text('Podの状態とVRAMを確認しています。')
        self.setup_btn.set_sensitive(False)
        self.cancel = bridge.ConnectionStop()
        cancel, generation, pod, folder = self.cancel, self.generation, self.pod, gpu.STORE
        def notify(stage, message):
            GLib.idle_add(self.update_state, generation, stage, message)
        def local_progress(message):
            if cancel.is_set():
                raise InterruptedError('接続先が変更されました。')
            notify('preparing', message)
        def run():
            try:
                state = gpu.rest(gpu.load_key(), '/v2/pods/'+pod)
                if state.get('id') != pod or state.get('status') != 'RUNNING':
                    notify('failed','Podは稼働していません。GPU一覧から起動できます。')
                    return
                if not state.get('gpu') or gpu.address_changed(state):
                    notify('failed','GPU Podの直接SSH接続が準備できてから再接続してください。')
                    return
                bridge.preflight(ssh)
                if cancel.is_set():
                    return
                if not strata_setup.source_paths():
                    strata_setup.setup_local(local_progress)
                if cancel.is_set():
                    return
                bridge.connect(pod, ssh, folder, cancel, notify)
            except Exception as error:
                notify('failed', str(error)[:180])
        threading.Thread(target=run, daemon=True).start()

    def update_state(self, generation, stage, value):
        if generation != self.generation or self.pod != gpu.target_settings().get('id'):
            return False
        if stage == 'ready':
            self.setup_btn.set_sensitive(True)
            model = gpu.target_settings().get('model') or 'GPU'
            self.indicator.set_text(f'○　{model}　·　Qwen IQ3_S 応答確認済み')
            if self.view is None:
                self.origin = urlsplit(value).netloc
                self.view = WebKit.WebView(network_session=WebKit.NetworkSession.new_ephemeral())
                self.view.connect('decide-policy', self.navigation)
                self.view.connect('load-failed', self.load_failed)
                self.pages.add_named(self.view,'chat')
            self.pages.set_visible_child_name('chat')
            self.view.load_uri(value)
        else:
            if stage == 'failed':
                self.setup_btn.set_sensitive(True)
            self.indicator.set_text('準備中' if stage == 'preparing' else 'GPU未接続')
            self.pages.set_visible_child_name('status')
            self.note.set_text(value)
            self.retry.set_visible(stage == 'failed')
        return False

    def navigation(self, view, decision, kind):
        if kind in (WebKit.PolicyDecisionType.NAVIGATION_ACTION, WebKit.PolicyDecisionType.NEW_WINDOW_ACTION):
            url = urlsplit(decision.get_navigation_action().get_request().get_uri())
            if kind == WebKit.PolicyDecisionType.NEW_WINDOW_ACTION or (url.scheme, url.netloc) != ('http',self.origin):
                decision.ignore()
                return True
        return False

    def load_failed(self, view, event, uri, error):
        if view is self.view and not error.matches(Gio.io_error_quark(),Gio.IOErrorEnum.CANCELLED):
            self.note.set_text('本家Strataの画面に接続できませんでした。')
            self.retry.set_visible(True)
            self.pages.set_visible_child_name('status')
        return False
