"""Runpod's email verification flow and desktop first-run setup."""
import base64
import hashlib
import json
import re
import secrets
import subprocess
import shutil
import uuid
from urllib.parse import quote

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

import gpu
import rental
from market import run_async

ATTEMPTS = "/v2/signup/authentication-attempts"


def request_code(email):
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) or len(email) > 254:
        raise ValueError("メールアドレスを確認してください。")
    prepare_login()
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    attempt = gpu.request(ATTEMPTS, {"email": email, "keyName": "RunPod GPU Desktop",
                                    "codeChallenge": challenge})
    return attempt["attemptId"], verifier


def verify_code(attempt, code):
    if not re.fullmatch(r"[0-9]{6}", code):
        raise ValueError("メールに届いた6桁の数字を入力してください。")
    identity, verifier = attempt
    reply = gpu.request(f"{ATTEMPTS}/{quote(identity, safe='')}/verify",
                        {"codeVerifier": verifier, "code": code})
    return reply["credential"]["apiKey"], reply["userId"]


def resend_code(attempt):
    identity, verifier = attempt
    gpu.request(f"{ATTEMPTS}/{quote(identity, safe='')}/resend", {"codeVerifier": verifier})


def available_pods(key):
    return [p for p in rental.instance_list(key) if p.get("gpu") and p.get("status") != "TERMINATED"
            and re.fullmatch("[a-z0-9]{8,32}", p.get("id", ""))]


def prepare_login():
    order = rental.read_order()
    if order and order["state"] not in rental.TERMINAL:
        raise ValueError("予約が進行中です。完了または取消の後でアカウントを接続し直せます。")


def credential_scope(key):
    return hashlib.sha256(("runpod-key\0" + key).encode()).hexdigest()


def account_scope(key):
    try:
        account_id = gpu.rest(key, "/v2/account").get("id")
    except gpu.ApiError as exc:
        if exc.status not in (401, 403, 404):
            raise
        account_id = None
    if not isinstance(account_id, str) or not account_id:
        return credential_scope(key)
    return hashlib.sha256(("runpod-account\0" + account_id).encode()).hexdigest()


def saved_login(scope):
    return gpu.PROFILE / "accounts" / scope / "login.json"


def forget_session():
    """Finish a committed sign-out, including after a failed keyring operation or interrupted logout."""
    profile = gpu.connection()
    if not profile.get("signed_out"):
        return
    slot = profile.get("pending_clear")
    if slot:
        attrs = {"pod":slot}
        gpu.Secret.password_clear_sync(gpu.SCHEMA, attrs, None)
        if gpu.Secret.password_lookup_sync(gpu.SCHEMA, attrs, None) is not None:
            raise RuntimeError("キーリングのサインアウトを完了できませんでした。")
    for name in ("reservation.pending","reservation.json","target.json","strata-ready.json"):
        (gpu.PROFILE/name).unlink(missing_ok=True)
    import pod_web
    for path in pod_web.console_paths(profile.get("old_browser", {})):
        if path.is_dir():
            shutil.rmtree(path)
    gpu.atomic_write(gpu.PROFILE/"connection.json", json.dumps({"signed_out":True}))
    gpu.reload_target()


def detach(key=None):
    """Called under the reservation lock. Never changes the power state of a Pod."""
    profile = gpu.connection()
    if profile.get("signed_out"):
        forget_session()
        return
    order = rental.read_order()
    if order and order["state"] not in rental.TERMINAL | {"waiting"}:
        raise ValueError("発注済みの予約を確認中です。割当処理が終わるとサインアウトできます。")
    if key is None:
        key = gpu.load_key()
    if key and profile.get("slot"):
        scope = profile.get("scope")
        if not scope:
            try:
                scope = account_scope(key)
            except (OSError, gpu.ApiError):
                scope = credential_scope(key)
        settings = {k:profile[k] for k in ("identity","legacy_pod") if k in profile}
        settings["legacy_store"] = profile.get("legacy_store", not profile.get("scope"))
        if order and order["state"] in rental.TERMINAL and order.get("pod"):
            settings["order"] = order
        # Record no API credential. Only the authenticated owner can re-adopt its SSH configuration.
        saved = saved_login(scope)
        saved.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        gpu.atomic_write(saved,json.dumps(settings))
    gpu.atomic_write(gpu.PROFILE/"active-host.conf",f"Host {gpu.HOST}\n    HostName pending.invalid\n")
    old_browser = {k:profile[k] for k in ("slot","scope") if k in profile}
    tombstone = {"signed_out":True, "pending_clear":profile.get("slot"), "old_browser":old_browser}
    gpu.atomic_write(gpu.PROFILE/"connection.json",json.dumps(tombstone))
    gpu.reload_target()
    forget_session()


def sign_out():
    with rental.locked():
        detach()


def bind_login(key, verified_user=None):
    # The verification response provides the identity and a one-time credential together.
    # Save it even if the new account's Pod list isn't available yet.
    if verified_user:
        scope = hashlib.sha256(("runpod-account\0" + verified_user).encode()).hexdigest()
    else:
        gpu.rest(key, "/v2/pods?limit=1")
        scope = account_scope(key)
    with rental.locked():
        prepare_login()
        old = gpu.connection()
        if old.get("signed_out"):
            forget_session()
        elif old.get("slot"):
            detach()
        stored = saved_login(scope)
        if not stored.is_file() and saved_login(credential_scope(key)).is_file():
            scope = credential_scope(key)
            stored = saved_login(scope)
        settings = json.loads(stored.read_text()) if stored.is_file() else {}
        profile = {k:settings[k] for k in ("legacy_store","legacy_pod","identity") if k in settings}
        profile.update(scope=scope, slot=uuid.uuid4().hex, configured=False)
        gpu.save_key(key, profile["slot"])
        try:
            gpu.atomic_write(gpu.PROFILE/"connection.json",json.dumps(profile))
        except OSError:
            gpu.Secret.password_clear_sync(gpu.SCHEMA, {"pod": profile["slot"]}, None)
            raise


def set_target(pod):
    with rental.locked():
        order = rental.read_order()
        if order and order["state"] not in rental.TERMINAL:
            if (pod or {}).get("id", "") != gpu.POD:
                raise ValueError("予約の処理中です。完了後に接続先を変更できます。")
        subprocess.run(["systemctl", "--user", "enable", "--now", "runpod-reservation.path"],
                       check=True, capture_output=True, timeout=15)
        if pod:
            model = (pod.get("gpu") or {}).get("id") or pod.get("name") or "GPU"
            gpu.atomic_write(gpu.target_file, json.dumps({"id":pod["id"], "model":model}))
        elif gpu.target_file.exists():
            gpu.target_file.unlink()
        gpu.reload_target()
        gpu.select_ssh_alias()
        profile = gpu.connection()
        scope = profile.get("scope")
        if scope and pod and not rental.read_order():
            saved = saved_login(scope)
            if saved.is_file():
                last = json.loads(saved.read_text()).get("order") or {}
                if last.get("pod") == pod["id"] and last.get("state") in rental.TERMINAL:
                    gpu.atomic_write(rental.ORDER,json.dumps(last))
        gpu.atomic_write(gpu.PROFILE / "connection.json", json.dumps(dict(profile, configured=True)))


def problem(exc, stage=None):
    if isinstance(exc, gpu.ApiError):
        if stage == "code":
            messages = {403:"このアカウントはRunPod公式画面でのログインが必要です。下の「公式画面で発行したキーを使う」から接続してください。",
                        404:"この確認コードは失効しました。「最初からやり直す」から新しいコードを受け取ってください。",
                        422:"確認コードが一致しません。最新のメールに届いた6桁を入力してください。",
                        429:"しばらく待ってから、もう一度お試しください。"}
            if exc.status in messages:
                return messages[exc.status]
        if stage == "pods" and exc.status in (401,403,429,500,502,503,504):
            if exc.status in (401,403):
                return "Pod一覧の読み取りが許可されていません。「Pod一覧を再取得」を試し、続く場合はPodsの読み取り権限を確認してください。"
            return "RunPodのPod一覧をまだ取得できません。「Pod一覧を再取得」から続けられます。"
        return {401:"キーが無効か失効しています。接続し直してください。",
                403:"メール認証またはこのキーでのPod操作が許可されていません。公式画面でPodsの読み書き用キーを発行して接続できます。",
                404:"RunPodの接続先が見つかりません。もう一度お試しください。",
                422:"RunPodが入力内容を受け付けませんでした。入力を確認してください。",
                429:"RunPodの受付回数に達しました。時間をおいて再試行してください。"}.get(
                    exc.status, f"RunPodに接続できませんでした（HTTP {exc.status}）。")
    if isinstance(exc, ValueError):
        return str(exc)
    return "接続または保存ができませんでした。ネットワークとこのPCのキーリングのロックを確認してください。"


class Account(Gtk.Box):
    def __init__(self, finish, credentials, logout=None, signup=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                         halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        self.set_size_request(465, -1)
        self.finish = finish
        self.credentials = credentials
        self.logout = logout
        self.signup = signup
        self.attempt = self.pending = None
        self.busy = False
        self.pods = []
        self.title = Gtk.Label(xalign=0)
        self.title.add_css_class("markettitle")
        self.append(self.title)
        self.fields = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=9)
        self.append(self.fields)
        self.note = Gtk.Label(xalign=0, wrap=True, max_width_chars=55)
        self.append(self.note)
        self.email = ""
        self.start()

    def form(self, title, detail, guide_hint):
        self.guide_hint = guide_hint
        self.title.set_text(title)
        while child := self.fields.get_first_child():
            self.fields.remove(child)
        self.note.set_text("")
        self.fields.append(Gtk.Label(label=detail, xalign=0, wrap=True, max_width_chars=55))

    def button(self, title, action, primary=False):
        btn = Gtk.Button(label=title)
        if primary:
            btn.add_css_class("action")
        btn.connect("clicked", lambda _: action())
        self.fields.append(btn)
        return btn

    def perform(self, task, done, progress, failed=None, stage=None):
        if self.busy:
            return
        self.busy = True
        self.fields.set_sensitive(False)
        self.note.set_text(progress)
        def safe_task():
            try:
                return task(), ""
            except Exception as exc:
                return None, problem(exc, stage)
        def complete(result, unexpected):
            self.busy = False
            self.fields.set_sensitive(True)
            value, error = result if result is not None else (None, "設定を再試行してください。")
            self.note.set_text(error)
            if not error:
                done(value)
            elif failed:
                failed()
        run_async(safe_task, complete)

    def start(self):
        self.attempt = None
        self.form("自分のRunPodに接続", "初回はメールで本人確認します。RunPodに未登録のアドレスは、確認後に新規アカウントになります。",
                  "自分のメールアドレスを入力して「確認コードをメールで受け取る」を押してください。各自の接続キーはこのPCに保存します。")
        email = Gtk.Entry(placeholder_text="RunPodで使うメールアドレス", text=self.email)
        self.fields.append(email)
        def send():
            self.email = email.get_text().strip()
            self.perform(lambda: request_code(self.email), self.code_form, "RunPodに確認コードを依頼しています…")
        self.button("確認コードをメールで受け取る", send, True)
        email.connect("activate", lambda _: send())
        self.button("制限付きAPIキー・組織アカウントで接続", self.manual)
        if self.signup:
            def official_signup():
                self.manual()
                self.signup()
            self.button("RunPod公式画面でアカウントを作成", official_signup)
        if self.logout and gpu.connection().get("slot"):
            self.button("サインアウト", self.logout)

    def code_form(self, attempt):
        self.attempt = attempt
        self.form("メールを確認", f"{self.email} に届いた6桁を入力してください。RunPodがアプリ用のキーを発行し、このPCに保存します。権限をPodsだけに限定したい場合は、下の「公式画面で発行したキーを使う」を選べます。",
                  "最新のメールにある6桁を入力して「本人確認して接続」を押してください。")
        code = Gtk.Entry(placeholder_text="6桁の確認コード", max_length=6,
                         input_purpose=Gtk.InputPurpose.DIGITS, visibility=False)
        self.fields.append(code)
        def verify():
            value = code.get_text().strip()
            self.perform(lambda: verify_code(attempt, value), lambda result: self.received(*result), "確認しています…", stage="code")
        self.button("本人確認して接続", verify, True)
        code.connect("activate", lambda _: verify())
        def resent(_):
            code.set_text("")
            self.note.set_text("新しいコードを送りました。最新のメールを確認してください。")
        self.button("確認コードを再送", lambda: self.perform(lambda: resend_code(attempt), resent, "再送しています…", stage="code"))
        self.button("最初からやり直す", self.start)
        self.button("公式画面で発行したキーを使う", self.manual)

    def manual(self):
        self.attempt = None
        self.form("APIキーで接続", "RunPodの「API Keys」で専用キーを発行し、Podsに読み書き権限を付けます。このPCのキーリングに保存します。",
                  "自分のRunPodで発行したキーを貼り付け、「このキーで接続」を押してください。")
        self.button("RunPodのキー発行画面を開く", self.credentials)
        entry = Gtk.PasswordEntry(placeholder_text="発行したAPIキー")
        self.fields.append(entry)
        def use():
            key = entry.get_text().strip()
            if not key:
                self.note.set_text("RunPodで発行したキーを入力してください。")
                return
            self.perform(lambda: bind_login(key), self.stored, "キーを確認して、このPCに保存しています…")
        self.button("このキーで接続", use, True)
        self.button("メールで接続", self.start)

    def received(self, key, verified_user):
        self.attempt = None
        self.pending = key
        self.form("このPCに接続を保存", "APIキーはOSのキーリングに、SSH鍵はこのPCの利用者専用フォルダーに保存します。",
                  "保存が終わるとGPUの確認に進みます。キーリングの解除を求められたら、このPCで解除してください。")
        def store():
            self.perform(lambda: bind_login(key, verified_user), self.stored, "キーリングに保存しています…")
        self.button("保存を再試行", store)
        store()

    def stored(self, _):
        self.pending = None
        self.discover()

    def discover(self, auto=True):
        self.form("RunPodとの接続", "ログイン情報は保存済みです。初回の接続が中断しても、この画面で再開できます。",
                  "Podは借りたGPUの実行環境です。初めてなら一覧は空で大丈夫です。取得に失敗したら「Pod一覧を再取得」を押してください。")
        self.button("Pod一覧を再取得", lambda: self.discover(auto))
        def find():
            key = gpu.load_key()
            if not key:
                raise ValueError("接続用のキーがありません。")
            gpu.prepare_connection()
            return available_pods(key)
        self.perform(find, lambda pods: self.choose(pods, auto), "Podを確認しています…",
                     lambda: self.button("本人確認からやり直す", self.start), stage="pods")

    def choose(self, pods, auto):
        self.pods = pods
        if auto and len(pods) <= 1:
            self.complete(pods[0] if pods else None)
            return
        self.form("RunPodに接続済み", "使いたいPodを選べます。新しくGPUを借りる場合は「GPU一覧へ」を選んでください。",
                  "新しく借りるときは「GPU一覧へ」、すでに借りているPodを使うときは「このPodを使う」を押してください。")
        if pods:
            labels = [f"{p.get('name') or 'Pod'}　·　{p['id']}" for p in pods]
            choices = Gtk.DropDown.new_from_strings(labels)
            choices.set_selected(next((i for i,p in enumerate(pods) if p['id']==gpu.POD), 0))
            self.fields.append(choices)
            self.button("このPodを使う", lambda: self.complete(pods[choices.get_selected()]), True)
        self.button("GPU一覧へ", lambda: self.complete(next((p for p in pods if p['id']==gpu.POD), None)))
        if self.logout:
            self.button("サインアウト", self.logout)

    def complete(self, pod):
        self.perform(lambda: set_target(pod), lambda _: self.finish(), "接続先を保存しています…",
                     lambda: self.button("接続先の保存を再試行", lambda: self.complete(pod)))
