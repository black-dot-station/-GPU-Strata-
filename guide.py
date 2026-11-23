"""Optional first-rental instructions alongside the controls that perform each step."""
from gi.repository import GLib, Gtk

import gpu

READY_TITLE = "GPUの接続を確認しました"
BILLING_NOTICE = "サインアウトしても、稼働中のPodの課金は続きます。"


class RentalGuide(Gtk.Box):
    def __init__(self, manager):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.manager = manager
        self.preference = gpu.PROFILE / "rental-guide"
        self.previous_ready = ""
        try:
            self.mode, _, self.previous_ready = self.preference.read_text().strip().partition(" ")
        except FileNotFoundError:
            self.mode = "off" if gpu.connection() else "offer"
        except OSError:
            self.mode = "off"
        self.add_css_class("rental-guide")
        copy = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
        self.heading = Gtk.Label(xalign=0, wrap=True)
        self.heading.add_css_class("heading")
        self.hint = Gtk.Label(xalign=0, wrap=True)
        copy.append(self.heading)
        copy.append(self.hint)
        self.append(copy)
        self.start_button = Gtk.Button(label="案内を始める", valign=Gtk.Align.CENTER)
        self.start_button.add_css_class("action")
        self.start_button.connect("clicked", self.start)
        self.append(self.start_button)
        self.dismiss = Gtk.Button(label="スキップ", valign=Gtk.Align.CENTER)
        self.dismiss.set_tooltip_text("メニューの「初めてGPUを借りる」から再表示できます。")
        self.dismiss.connect("clicked", lambda _: self.set_mode("off"))
        self.append(self.dismiss)
        self.refresh()

    def set_mode(self, mode):
        self.mode = mode
        try:
            self.preference.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            value = f"active {self.previous_ready}" if mode == "active" and self.previous_ready else mode
            gpu.atomic_write(self.preference, value + "\n")
        except OSError:
            Gtk.AlertDialog(message="案内の設定をこのPCに保存できませんでした。今回の起動には選択を反映します。").show(self.manager)
        self.refresh()

    def start(self, *_):
        order = self.manager.market.order or {}
        self.previous_ready = order.get("name", "") if order.get("state") == "ready" else ""
        self.set_mode("active")
        page = self.manager.stack.get_visible_child_name()
        if page not in ("account", "market") and not self.manager.console_return:
            target = "market" if self.manager.account_ready else "account"
            self.manager.activate_action("win.page", GLib.Variant("s", target))

    def refresh(self, *_):
        self.set_visible(self.mode in ("offer", "active"))
        if not self.get_visible():
            return
        offering = self.mode == "offer"
        self.start_button.set_visible(offering)
        if offering:
            title, hint = "初めてGPUを借りる方へ", "自分のRunPodに接続して、GPUを選び、使い終わったら停止するまで案内します。"
        else:
            title, hint = self.current_step()
        self.heading.set_text(title)
        self.hint.set_markup(GLib.markup_escape_text(hint).replace(BILLING_NOTICE, f"<b>{BILLING_NOTICE}</b>"))
        self.dismiss.set_label("案内を終える" if title == READY_TITLE else "スキップ")

    def current_step(self):
        w = self.manager
        page = w.stack.get_visible_child_name()
        if page == "console" and w.console_return == "market":
            return "残高を準備する", "支払いはRunPodの公式画面で行います。終わったら右上の「残高を再確認して戻る」を押してください。"
        if page == "console" and w.console_return == "account":
            return "自分のRunPodに接続する", "公式画面で手続きを済ませたら「接続設定に戻る」を押してください。"
        if page == "account":
            return "自分のRunPodに接続する", w.account_page.guide_hint
        if not w.account_ready:
            return "自分のRunPodに接続する", "「メニュー」から「RunPodの接続設定」を開いてください。"
        order = w.market.order or {}
        state = order.get("state")
        if state == "waiting":
            return "予約を受け付けました", "上限内で空きが確保できると自動で起動します。空き待ちの間は「予約を取消」で取り消せます。"
        if state in ("submitted", "booting"):
            return "GPUの準備を待つ", "起動や接続の進み具合は「GPU一覧・予約」で確認できます。使える状態になると作業画面が開きます。"
        if state == "stopping":
            return "自動停止を確認中", "「GPU一覧・予約」で停止の理由と結果を確認してください。"
        if state == "ready" and order.get("name") != self.previous_ready:
            if order.get("pod") and order["pod"] == gpu.target_settings().get("id"):
                return READY_TITLE, "使い終わったら「メニュー」→「Podの電源・状態」→「停止」。" + BILLING_NOTICE
        if page != "market":
            return "GPU一覧を開く", "「メニュー」→「GPU一覧・予約」で機種と1時間の料金を選べます。"
        if order.get("recovery") == "account":
            return "接続設定を確認する", "「接続設定を開く」でアカウントとキーの権限を確認してください。"
        if w.market.billing_bar.get_visible() and w.market.balance_loading:
            return "残高を確認中", "RunPodから残高を取得しています。"
        if w.market.billing_bar.get_visible() and w.market.balance_empty:
            return "残高を準備する", "「RunPodの支払い設定」で選んだGPUの1時間分の残高を用意し、「残高を再確認」を押してください。"
        if order.get("recovery") == "billing":
            return "支払い設定と料金を確認する", "「RunPodの支払い設定」で残高を確認できたら、予算に合うGPUをもう一度選んでください。"
        return "GPUを選んで借りる", "「料金上限 / 時間」を選びます。上限内なら○は「今すぐ起動」、×は「予約して自動起動」。" + BILLING_NOTICE
