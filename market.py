"""Native stock list, filters and one-click reservation controls."""
from datetime import datetime
from decimal import Decimal
from time import time
import threading
from gi.repository import Gdk, GLib, GObject, Gtk, Pango
import inventory
import rental


def text(value, style="", *, width=None):
    obj = Gtk.Label(label=value, xalign=0)
    if style:
        obj.add_css_class(style)
    if width:
        obj.set_size_request(width, -1)
    return obj


def row(spacing=10):
    return Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=spacing)


def run_async(fn, callback):
    def work():
        try:
            value, error = fn(), ""
        except Exception as exc:
            value, error = None, str(exc)
        GLib.idle_add(callback, value, error)
    threading.Thread(target=work, daemon=True).start()


def dropdown(items, selected=0):
    widget = Gtk.DropDown.new_from_strings(items)
    widget.set_selected(selected)
    return widget


def on_screen(widget):
    if not widget.get_mapped():
        return False
    surface = widget.get_native().get_surface()
    return bool(surface and not surface.get_state() & Gdk.ToplevelState.MINIMIZED)


class Market(Gtk.Box):
    __gsignals__ = {"order-changed": (GObject.SignalFlags.RUN_LAST, None, ())}

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.set_margin_start(18)
        self.set_margin_end(18)
        self.set_margin_bottom(12)
        self.offers = []
        self.order = None
        self.fresh = False
        self.loading = False
        self.changing = False
        self.require_account = None
        self.last_success = 0
        self.next_check = 0
        self.cache_stamp = None
        self.closed = False
        self.last_error = ""
        self.first_gpu = False
        self.balance_loading = False
        self.balance_empty = False
        header = row()
        title = text("GPUの空き状況", "markettitle")
        title.set_hexpand(True)
        header.append(title)
        self.updated = text("在庫を取得中…", "caption")
        header.append(self.updated)
        self.append(header)
        self.header = header

        self.billing_bar = row(8)
        self.balance_label = text("", "caption")
        self.balance_label.set_wrap(True)
        self.balance_label.set_hexpand(True)
        self.billing_bar.append(self.balance_label)
        self.billing_btn = Gtk.Button(label="RunPodの支払い設定")
        self.billing_bar.append(self.billing_btn)
        self.balance_btn = Gtk.Button(label="残高を再確認")
        self.balance_btn.connect("clicked", lambda _: self.check_balance())
        self.billing_bar.append(self.balance_btn)
        self.billing_bar.set_visible(False)
        self.append(self.billing_bar)

        filters = row(7)
        self.search = Gtk.SearchEntry(placeholder_text="機種名で検索", hexpand=True)
        filters.append(self.search)
        self.cloud = dropdown(["両クラウド", "Community", "Secure"])
        self.vram = dropdown(["全容量", "24 GB以上", "48 GB以上", "80 GB以上"], 2)
        self.sort = dropdown(["料金が安い順", "VRAMが多い順", "機種名順", "空き優先"])
        for control in (self.cloud, self.vram, self.sort):
            filters.append(control)
        self.append(filters)

        limits = row(7)
        self.summary = text("", "caption")
        self.summary.set_hexpand(True)
        limits.append(self.summary)
        self.allow_jupyter = Gtk.CheckButton(label="直接SSHが使えない時はJupyterで作業")
        self.allow_jupyter.set_tooltip_text("JupyterでGPU計算が成功したら作業画面を開きます。ローカルのスクリプト転送には直接SSHが必要です。")
        limits.append(self.allow_jupyter)
        limits.append(text("料金上限 / 時間", "caption"))
        self.limits = tuple(map(Decimal, ("0.34", "0.50", "1.00", "2.00", "5.00", "10.00")))
        self.budget = dropdown([f"${limit:.2f}" for limit in self.limits])
        limits.append(self.budget)
        self.append(limits)
        table = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        table.add_css_class("stocktable")
        cols = row()
        cols.add_css_class("tablehead")
        self.cells(cols, ["在庫","GPUモデル / クラウド","VRAM","合計 / 時間","操作"])
        table.append(cols)
        self.listing = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.listing.add_css_class("stocklist")
        self.scroller = Gtk.ScrolledWindow(vexpand=True, min_content_height=158)
        self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroller.set_child(self.listing)
        table.append(self.scroller)
        self.append(table)

        self.tray = row()
        self.tray.add_css_class("ordertray")
        self.progress = Gtk.Spinner()
        self.tray.append(self.progress)
        self.message = text("")
        self.message.set_wrap(True)
        self.message.set_hexpand(True)
        self.tray.append(self.message)
        self.try_lab = Gtk.Button(label="このPodをJupyterで確認")
        self.try_lab.connect("clicked", self.check_current_lab)
        self.tray.append(self.try_lab)
        self.undo = Gtk.Button(label="予約を取消")
        self.undo.connect("clicked", lambda _: self.act(rental.cancel))
        self.tray.append(self.undo)
        self.account_btn = Gtk.Button(label="接続設定を開く")
        self.tray.append(self.account_btn)
        self.append(self.tray)
        self.note = text("50 GBディスク込み。起動後は停止まで課金。発注直前に上限を確認し、割当額が上限を超えた場合は自動停止します（停止までの料金がかかります）。", "caption")
        self.note.set_wrap(True)
        self.append(self.note)
        self.search.connect("search-changed", lambda _: self.render())
        for control in (self.cloud,self.vram,self.sort,self.budget):
            control.connect("notify::selected", lambda *_: self.render())
        self.update_order()
        self.connect("map", lambda *_: self.update_order())
        self.watch = GLib.timeout_add_seconds(1, self.update_order)

    def shutdown(self):
        self.closed = True
        GLib.source_remove(self.watch)

    def show_billing(self, first_gpu):
        self.first_gpu = first_gpu
        self.billing_bar.set_visible(first_gpu or (self.order or {}).get("recovery") == "billing")
        if self.billing_bar.get_visible():
            self.check_balance()
        else:
            self.balance_empty = False

    def check_balance(self):
        if self.closed or self.balance_loading or not self.billing_bar.get_visible():
            return
        self.balance_loading = True
        self.balance_btn.set_sensitive(False)
        self.balance_label.set_text("RunPodの残高を確認中…")
        self.render()

        def received(result, error):
            self.balance_loading = False
            if self.closed:
                return
            self.balance_btn.set_sensitive(True)
            if not self.billing_bar.get_visible():
                return
            self.balance_empty = False
            if error or not result:
                message = "このキーでは残高を確認できません。公式の支払い設定で確認できます。"
            elif result[0] == "POSTPAID":
                message = "後払いアカウントです。GPUを選べます。"
            elif result[0] == "PREPAID":
                amount = result[1]
                self.balance_empty = amount <= 0
                message = (f"残高 ${amount:.2f}。入金後に「残高を再確認」を押してください。" if self.balance_empty else
                           f"残高 ${amount:.2f}。選んだGPUを1時間使える残高が必要です。")
            else:
                message = "公式の支払い設定で請求方法を確認できます。"
            self.balance_label.set_text(("接続済み・最初のGPU　" if self.first_gpu else "") + message)
            self.render()
        run_async(rental.billing_balance, received)

    def cells(self, parent, values):
        for value,width in zip(values,(76,None,72,96,153)):
            cell = text(value) if isinstance(value, str) else value
            if width:
                cell.set_size_request(width,-1)
            else:
                cell.set_hexpand(True)
            parent.append(cell)

    def fetch(self, manual=False):
        if self.closed or self.loading or not on_screen(self):
            return
        self.loading = True
        if not self.last_success or manual:
            self.updated.set_text("在庫を確認中…")
        def received(view,error):
            self.loading = False
            if self.closed:
                return
            old_fresh = self.fresh
            old_error = self.last_error
            changed = False
            now = time()
            if error:
                self.fresh = False
                self.next_check = now + inventory.NORMAL_INTERVAL
                self.last_error = "更新エラー"
            else:
                changed = self.offers != view.offers
                self.offers = view.offers
                self.last_success = view.checked
                self.next_check = view.due
                self.fresh = view.is_fresh(now)
                self.last_error = view.error
            self.update_stamp()
            if changed or old_fresh != self.fresh or old_error != self.last_error:
                self.render()
        run_async(lambda: inventory.snapshot(manual=manual), received)

    def update_stamp(self):
        def stamp(value):
            when = datetime.fromtimestamp(value)
            fmt = "%H:%M:%S" if when.date() == datetime.now().date() else "%m/%d %H:%M"
            return when.strftime(fmt)
        last = f"{stamp(self.last_success)} 取得" if self.last_success else "未取得"
        status = f"{self.last_error}・再試行" if self.last_error else "次回"
        self.updated.set_text(f"{last}　·　{status} {stamp(self.next_check)}")

    def update_order(self):
        try:
            order = rental.read_order()
        except (OSError, ValueError):
            self.message.set_text("予約情報を読み込めませんでした。")
            return True
        self.set_order(order)
        if not on_screen(self):
            return True
        now = time()
        if self.fresh and not 0 <= now - self.last_success <= inventory.STALE_AFTER:
            self.fresh = False
            self.render()
            self.update_stamp()
        try:
            stamp = (inventory.ROOT / "inventory.json").stat().st_mtime_ns
        except OSError:
            stamp = None
        if not self.loading and (stamp != self.cache_stamp or now < self.last_success or now >= self.next_check):
            self.cache_stamp = stamp
            self.fetch()
        return True

    def set_order(self, order):
        if self.order == order:
            return
        previous = self.order or {}
        self.order = order
        changed = any(previous.get(k) != order.get(k) for k in ("state", "name", "pod"))
        self.order_text()
        if previous.get("recovery") != order.get("recovery"):
            self.show_billing(self.first_gpu)
        if changed:
            self.render()
            self.emit("order-changed")

    def order_text(self):
        order = self.order or {}
        state = order.get("state")
        self.undo.set_visible(state == "waiting")
        self.account_btn.set_visible(order.get("recovery") == "account")
        self.undo.set_sensitive(not self.changing)
        self.try_lab.set_visible(state == "booting" and not order.get("allow_jupyter"))
        self.try_lab.set_sensitive(not self.changing)
        self.progress.set_visible(state in ("submitted", "booting"))
        self.progress.set_spinning(self.progress.get_visible())
        self.reorder_child_after(self.tray, self.header if state and state not in rental.TERMINAL else self.scroller.get_parent())
        if order:
            receipt = "予約済み　" if state == "waiting" else ""
            self.message.set_text(f"{order['title']}　{receipt}{order['message']}")
        if state == "ready":
            self.message.set_text(f"{order['title']}　{order['message']} 作業画面の接続先を確認しています。")
        elif not order or state == "cancelled":
            prefix = "予約を取り消しました。 " if state == "cancelled" else ""
            self.message.set_text(prefix + "予約すると、GPUの起動後に作業画面が自動で開きます。")

    def check_current_lab(self, _):
        name = (self.order or {}).get("name")
        if name:
            self.act(lambda: rental.enable_jupyter(name))

    def act(self, fn):
        if self.changing:
            return
        self.changing = True
        self.render()
        self.undo.set_sensitive(False)
        def done(_,error):
            self.changing = False
            self.set_order(rental.read_order())
            self.undo.set_sensitive(True)
            if error:
                self.message.set_text(error)
            self.render()
        run_async(fn, done)

    def choose_offer(self, offer, budget):
        if self.changing or offer.available is None:
            return
        if self.require_account and not self.require_account():
            return
        fallback = self.allow_jupyter.get_active()

        def queue(limit):
            if self.closed or (self.require_account and not self.require_account()):
                return
            if self.billing_bar.get_visible() and (self.balance_loading or self.balance_empty):
                return
            # Accept the displayed ceiling. The worker revalidates stock and price before purchase.
            if (self.fresh and 0 <= time() - self.last_success <= inventory.STALE_AFTER and
                    not any((v.model, v.cloud) == (offer.model, offer.cloud) for v in self.offers)):
                self.message.set_text("選んだ機種が一覧から消えました。機種を選び直してください。")
                return
            self.message.set_text(f"{offer.name} の予約を受け付けています…")
            self.reorder_child_after(self.tray, self.header)
            self.act(lambda: rental.reserve(offer, limit, allow_jupyter=fallback))

        if offer.hourly <= budget:
            queue(budget)
            return

        verb = "起動" if offer.available else "予約"
        rate = f"${offer.hourly:.2f}/時"
        dialog = Gtk.AlertDialog(
            message=f"{offer.name} を {rate} で{verb}しますか？",
            detail=(f"{offer.cloud.title()}・{offer.memory} GB・ディスク込み。"
                    f"現在の料金上限は ${budget:.2f}/時です。今回の1台だけ上限を {rate} にします。\n"
                    "空き次第、自動で起動します。起動すると停止まで料金がかかります。"))
        dialog.set_buttons(["キャンセル", f"{rate} で{verb}"])
        dialog.set_cancel_button(0)
        dialog.set_default_button(0)

        def chosen(alert, result):
            try:
                if alert.choose_finish(result) != 1:
                    return
            except GLib.Error:
                return
            queue(offer.hourly)

        dialog.choose(self.get_root(), None, chosen)

    def render(self):
        if not hasattr(self, "note"):
            return
        self.listing.remove_all()
        needle = self.search.get_text().strip().casefold()
        cloud = (None,"COMMUNITY","SECURE")[self.cloud.get_selected()]
        memory = (0,24,48,80)[self.vram.get_selected()]
        rows = [o for o in self.offers if (not cloud or o.cloud==cloud) and o.memory>=memory
                and needle in (o.model+" "+o.name).casefold()]
        keys = (lambda o:(o.hourly,o.name), lambda o:(-o.memory,o.hourly,o.name),
                lambda o:(o.name.casefold(),o.hourly), lambda o:(o.available is not True,o.hourly,o.name))
        rows.sort(key=keys[self.sort.get_selected()])
        count = sum(o.available is True for o in rows)
        full = sum(o.available is False for o in rows)
        budget = self.limits[self.budget.get_selected()]
        affordable = sum(o.available is True and o.hourly <= budget for o in rows)
        self.summary.set_text(f"○ 空き {count}件（上限内 {affordable}件）　× 満室 {full}件" if self.fresh else
                              (f"{self.last_error}のため在庫は不明です。自動で再確認します。" if self.last_error else "在庫を確認しています…"))
        active = bool(self.order and self.order.get("state") not in rental.TERMINAL)
        for offer in rows:
            line = row()
            line.add_css_class("stockrow")
            available = offer.available if self.fresh else None
            status = {True:("○  空き","stockyes"),False:("×  満室","stockno"),None:("―  不明","caption")}[available]
            model = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            name = text(offer.name,"stockname")
            name.set_ellipsize(Pango.EllipsizeMode.END)
            model.append(name)
            model.append(text(offer.cloud.title(),"caption"))
            action = Gtk.Button(label="今すぐ起動" if available else "予約して自動起動")
            within_budget = offer.hourly <= budget
            if not within_budget:
                action.set_label("料金を確認して起動" if available else "料金を確認して予約")
            if available is None:
                action.set_label("在庫確認待ち")
            if active:
                same = (offer.model,offer.cloud)==(self.order["model"],self.order["cloud"])
                action.set_label("受付中" if same else "別の予約が進行中")
            billing_blocked = self.billing_bar.get_visible() and (self.balance_loading or self.balance_empty)
            action.set_sensitive(available is not None and not active and not self.changing and not billing_blocked)
            if within_budget and offer.model==rental.PREFERRED and offer.cloud=="COMMUNITY":
                action.add_css_class("action")
            if not active and available is not None:
                tip = (f"空き次第、上限 ${offer.hourly:.2f}/時で1台起動します。" if within_budget else
                       f"上限 ${budget:.2f}/時を超えています。今回の料金を確認できます。")
                action.set_tooltip_text(tip)
            action.connect("clicked",lambda _,o=offer,b=budget: self.choose_offer(o,b))
            self.cells(line,[text(*status),model,text(f"{offer.memory} GB"),text(f"${offer.hourly:.2f}"),action])
            self.listing.append(line)
        if not rows:
            empty = text("該当する機種がありません。" if self.fresh else "在庫の取得を待っています。")
            empty.set_margin_start(14)
            empty.set_margin_top(14)
            self.listing.append(empty)
