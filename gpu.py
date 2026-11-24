"""Desktop and command-line access to the current user's RunPod."""
import base64
import csv
import datetime
import fcntl
import hashlib
import http.client
import ipaddress
import json
import os
import re
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import uuid

import gi
gi.require_version("Secret", "1")
from gi.repository import GLib, Secret

PROFILE = Path(GLib.get_user_config_dir()) / "runpod-desktop"
target_file = PROFILE / "target.json"
HOST = "runpod-appraisal"
def connection():
    path = PROFILE / "connection.json"
    return json.loads(path.read_text()) if path.exists() else {}


def account_folder(base=None):
    root = PROFILE if base is None else base
    profile = connection()
    scope = profile.get("scope")
    if not scope or profile.get("legacy_store"):
        return root
    if not re.fullmatch(r"[a-f0-9]{64}", scope):
        raise ValueError("RunPodアカウントの保存先を確認してください。")
    return root / "accounts" / scope


def target_settings():
    if connection().get("signed_out"):
        return {}
    settings = json.loads(target_file.read_text()) if target_file.exists() else {}
    if settings and not re.fullmatch("[a-z0-9]{8,32}", settings.get("id", "")):
        raise ValueError("Pod IDの形式を確認してください。")
    return settings


def reload_target():
    global POD, MODEL, STORE, IDENTITY, LEGACY_POD
    config = connection()
    settings = target_settings()
    LEGACY_POD = config.get("legacy_pod")
    POD = settings.get("id", "")
    MODEL = settings.get("model", "Pod未選択")
    root = account_folder()
    STORE = root if POD and POD == LEGACY_POD else root / "pods" / POD
    IDENTITY = Path(config.get("identity", root / "ssh" / "id_ed25519"))
reload_target()
SCHEMA = Secret.Schema.new("jp.local.RunpodDesktop", Secret.SchemaFlags.NONE,
                           {"pod": Secret.SchemaAttributeType.STRING})
DOWNLOAD = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD)
RESULTS = Path(DOWNLOAD or Path.home() / "Downloads") / "RunPod実行結果"
QUERY = "name,utilization.gpu,memory.used,memory.total,temperature.gpu"
TEST = """import torch
a = torch.ones((512, 512), device="cuda")
b = a @ a
torch.cuda.synchronize()
assert (b == 512).all().item()
print(f'{torch.cuda.get_device_name(0)}  /  PyTorch {torch.__version__}')
print('512 × 512 の行列積をGPUで計算し、全要素の一致を確認しました。')
"""


def load_key():
    slot = connection().get("slot")
    return Secret.password_lookup_sync(SCHEMA, {"pod": slot}, None) if slot else None


def save_key(key, slot=None):
    slot = slot or connection()["slot"]
    if not Secret.password_store_sync(SCHEMA, {"pod": slot}, Secret.COLLECTION_DEFAULT,
                                      "RunPod · GPU Desktop", key, None):
        raise RuntimeError("キーリングに保存できませんでした。")


class ApiError(RuntimeError):
    def __init__(self, status, detail):
        self.status = status
        self.detail = detail
        super().__init__(f"RunPod HTTP {status}  {detail}")


def api(key, resource="", payload=None):
    if not POD:
        raise ValueError("GPU一覧から使うGPUを選んでください。")
    return rest(key, f"/v2/pods/{POD}{resource}", payload)


def rest(key, path, payload=None):
    if not key:
        raise ValueError("電源設定でRunPodのAPIキーを登録してください。")
    return request(path, payload, key=key)


def request(path, payload=None, *, key=None):
    client = http.client.HTTPSConnection("api.runpod.io", timeout=18)
    try:
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = "Bearer " + key
        encoded = json.dumps(payload).encode() if payload is not None else None
        client.request("POST" if encoded else "GET", path,
                       body=encoded, headers=headers)
        response = client.getresponse()
        body = response.read()
        if response.status >= 300:
            try:
                data = json.loads(body)
                detail = str(data.get("detail", ""))
                errors = data.get("errors")
                if isinstance(errors, list) and errors:
                    # Validation failures carry the actual reason here, not in "detail".
                    detail = (detail + " " + " / ".join(map(str, errors))).strip()
                detail = detail[:300]
            except (ValueError, AttributeError):
                detail = response.reason
            raise ApiError(response.status, detail)
        return json.loads(body)
    finally:
        client.close()


def change_power(key, action):
    if action == "start":
        import rental
        with rental.locked():
            order = rental.read_order()
            if order and order["state"] not in rental.TERMINAL:
                raise RuntimeError("予約が進行中です。「GPU一覧・予約」で状態を確認してください。")
            return request_power(key, action, order)
    return request_power(key, action)


def request_power(key, action, order=None):
    if action not in ("start", "stop"):
        raise ValueError("未対応の操作です。")
    state = api(key)
    if action not in state.get("actions", []):
        raise RuntimeError("現在の状態ではこの操作を実行できません。更新してください。")
    if action == "start" and order is not None and state.get("id") == POD:
        name = state.get("name") or ""
        if re.fullmatch(r"desktop-gpu-[0-9a-f]{16}", name) and (account_folder() / "pairing" / name).is_file():
            import rental
            return rental.restart(key, state, order)
    return api(key, "/action", {"action": action})


def ssh_options():
    if not POD:
        raise ValueError("接続するPodが選択されていません。")
    opts = ["-oBatchMode=yes", "-oConnectTimeout=7", "-oServerAliveInterval=15",
            "-oServerAliveCountMax=3", "-oStrictHostKeyChecking=yes", "-oForwardAgent=no"]
    if POD != LEGACY_POD:
        opts += ["-F", "/dev/null", "-oUser=root", "-oIdentitiesOnly=yes", "-i", str(IDENTITY)]
    saved = STORE / "endpoint.json"
    if saved.exists():
        cfg = json.loads(saved.read_text())
        opts += endpoint_options(cfg["ip"], cfg["port"], cfg["pin"])
    elif POD != LEGACY_POD:
        raise ValueError("新しいPodのSSH接続を準備中です。")
    return opts


def address_changed(state):
    direct = (state.get("ssh") or {}).get("direct") or {}
    if not direct.get("host") or not direct.get("port"):
        return True
    saved = STORE / "endpoint.json"
    if saved.exists():
        config = json.loads(saved.read_text())
        host, port = config["ip"], config["port"]
    elif POD != LEGACY_POD:
        return True
    else:
        config = dict(line.split(" ", 1) for line in run(["ssh", "-G", HOST]).splitlines() if " " in line)
        host, port = config["hostname"], config["port"]
    return host != direct["host"] or int(port) != int(direct["port"])


def endpoint_options(ip, port, pin):
    ip = ipaddress.ip_address(ip)
    port = int(port)
    if not 0 < port < 65536 or len(pin) != 64 or any(c not in "0123456789abcdef" for c in pin):
        raise ValueError("SSH接続先の形式を確認してください。")
    return [f"-oHostName={ip}", f"-oPort={port}", f"-oHostKeyAlias={HOST}",
            f"-oUserKnownHostsFile={STORE / pin}"]


def atomic_write(path, text):
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as temp:
        temp.write(text)
    os.replace(temp.name, path)


def select_ssh_alias():
    text = f"Host {HOST}\n    HostName pending.invalid\n"
    endpoint = STORE / "endpoint.json"
    if POD and endpoint.is_file():
        peer = json.loads(endpoint.read_text())
        ip, port, pin = peer["ip"], peer["port"], peer["pin"]
        endpoint_options(ip, port, pin)
        if (STORE / pin).is_file() and IDENTITY.is_file():
            text = (f"Host {HOST}\n    HostName {ip}\n    Port {int(port)}\n    User root\n"
                    f"    HostKeyAlias {HOST}\n    UserKnownHostsFile {json.dumps(str(STORE/pin), ensure_ascii=False)}\n"
                    f"    IdentityFile {json.dumps(str(IDENTITY), ensure_ascii=False)}\n"
                    "    IdentitiesOnly yes\n    StrictHostKeyChecking yes\n    ForwardAgent no\n")
    atomic_write(PROFILE / "active-host.conf", text)


def prepare_connection():
    PROFILE.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (PROFILE / "setup.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if connection().get("signed_out"):
            raise ValueError("RunPodにサインインしてください。")
        if not connection():
            atomic_write(PROFILE / "connection.json", json.dumps({"slot": uuid.uuid4().hex}))
        reload_target()
        if IDENTITY.is_file():
            if not IDENTITY.with_suffix(".pub").is_file():
                public = run(["ssh-keygen", "-y", "-P", "", "-f", str(IDENTITY)])
                atomic_write(IDENTITY.with_suffix(".pub"), public + "\n")
            return
        if connection().get("identity"):
            raise ValueError("このPCで設定したSSH鍵が見つかりません。")
        with tempfile.TemporaryDirectory(dir=PROFILE) as temp:
            pair = Path(temp) / "keys"
            pair.mkdir(mode=0o700)
            run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "runpod-desktop",
                 "-f", str(pair / "id_ed25519")])
            IDENTITY.parent.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.replace(pair, IDENTITY.parent)


def recovery_command():
    public = IDENTITY.with_suffix(".pub").read_text().strip()
    return (f'test "$RUNPOD_POD_ID" = {POD} && (umask 077; mkdir -p ~/.ssh && '
            f'touch ~/.ssh/authorized_keys && (grep -qxF {shlex.quote(public)} ~/.ssh/authorized_keys || '
            f"printf '\\n%s\\n' {shlex.quote(public)} >> ~/.ssh/authorized_keys) && "
            'cat /etc/ssh/ssh_host_ed25519_key.pub)')


def reconnect(key, host_key=None):
    state = api(key)
    if POD != LEGACY_POD and host_key is None and (account_folder() / "pairing" / str(state.get("name", ""))).is_file():
        import rental
        rental.pair(state, {"name": state["name"]})
        return
    direct = (state.get("ssh") or {}).get("direct") or {}
    if state.get("id") != POD or not direct.get("host") or not direct.get("port"):
        raise RuntimeError("PodのSSH接続先を取得できません。起動が完了してから再接続してください。")
    ip, port = str(ipaddress.ip_address(direct["host"])), int(direct["port"])
    if host_key is None:
        saved = STORE / "endpoint.json"
        pin_file = (Path.home() / ".ssh" / f"runpod_{POD}.known_hosts")
        if saved.exists():
            prior = json.loads(saved.read_text())
            endpoint_options(prior["ip"], prior["port"], prior["pin"])
            pin_file = STORE / prior["pin"]
        parts = pin_file.read_text().splitlines()[0].split()
        fields = parts[1:3]
    else:
        fields = host_key.strip().split()
    if len(fields) < 2 or fields[0] != "ssh-ed25519":
        raise ValueError("Podのターミナルに表示された ssh-ed25519 から始まる公開鍵を貼ってください。")
    blob = base64.b64decode(fields[1], validate=True)
    if len(blob) != 51 or not blob.startswith(bytes.fromhex("0000000b") + b"ssh-ed25519" + bytes.fromhex("00000020")):
        raise ValueError("Ed25519ホスト公開鍵の形式を確認してください。")
    host_line = f"{HOST} {fields[0]} {fields[1]}\n"
    pin = hashlib.sha256(host_line.encode()).hexdigest()
    STORE.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write(STORE / pin, host_line)
    opts = endpoint_options(ip, port, pin)
    config_args = [] if POD == LEGACY_POD else ["-F", "/dev/null"]
    run(["ssh", *config_args, *opts, "-oBatchMode=yes", "-oStrictHostKeyChecking=yes", "-oConnectTimeout=7",
         "-oUser=root", "-oIdentitiesOnly=yes", "-i", str(IDENTITY), HOST, "true"])
    atomic_write(STORE / "endpoint.json", json.dumps(dict(ip=ip, port=port, pin=pin)))


def run(argv, *, timeout=20, source=None):
    result = subprocess.run(argv, capture_output=True, text=True, errors="replace", input=source,
                            timeout=timeout, stdin=subprocess.DEVNULL if source is None else None)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or "接続に失敗しました。").strip()[-1600:])
    return result.stdout.strip()


def ssh():
    return ["ssh", *ssh_options(), HOST]


def proxy_shell(state):
    proxy = (state.get("ssh") or {}).get("proxy") or {}
    if (not POD or state.get("id") != POD or state.get("status") != "RUNNING"
            or proxy.get("host") != "ssh.runpod.io"):
        return None
    user, port = proxy.get("username"), proxy.get("port")
    if (not isinstance(user, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", user)
            or not isinstance(port, int) or not 0 < port <= 65535):
        return None
    return ["ssh", "-F", "/dev/null", "-t", "-oForwardAgent=no", "-oClearAllForwardings=yes",
            "-oStrictHostKeyChecking=ask", "-oIdentitiesOnly=yes", "-oConnectTimeout=7",
            "-i", str(IDENTITY), "-p", str(port), user + "@ssh.runpod.io"]


def fingerprint(path):
    """SHA256 fingerprint of a public key or known-hosts file, or None when it cannot be read."""
    try:
        line = run(["ssh-keygen", "-l", "-E", "sha256", "-f", str(path)], timeout=5).splitlines()[0]
        return line.split()[1]
    except (OSError, RuntimeError, subprocess.SubprocessError, IndexError):
        return None


def ssh_details():
    """The key, address and pinned host key that this PC uses for the selected Pod."""
    public = IDENTITY.with_suffix(".pub")
    info = {"pod": POD, "identity": str(IDENTITY), "identity_fp": None,
            "address": None, "host_fp": None, "command": None}
    if public.is_file():
        info["identity_fp"] = fingerprint(public)
    saved = STORE / "endpoint.json"
    if POD and saved.is_file():
        cfg = json.loads(saved.read_text())
        endpoint_options(cfg["ip"], cfg["port"], cfg["pin"])
        info["address"] = f"{cfg['ip']}:{int(cfg['port'])}"
        if (STORE / cfg["pin"]).is_file():
            info["host_fp"] = fingerprint(STORE / cfg["pin"])
        info["command"] = shlex.join(ssh())
    return info


def ssh_check():
    """Log in with this app's own settings and return the Pod ID that answered and the time taken."""
    started = time.monotonic()
    reply = run([*ssh(), 'if test -r /etc/rp_environment; then . /etc/rp_environment; fi; '
                 'printf %s "$RUNPOD_POD_ID"'], timeout=20)
    return reply.strip(), time.monotonic() - started


def telemetry():
    row = next(csv.reader([run([*ssh(), f"nvidia-smi --id=0 --query-gpu={QUERY} --format=csv,noheader,nounits"])]))
    name, usage, used, total, temp = [item.strip() for item in row]
    return name, int(usage), int(used), int(total), int(temp)


def gpu_test():
    return run([*ssh(), "python3 -"], source=TEST, timeout=45)


def transfer():
    return ["rsync", "-rt", "--protect-args", "--no-links", "--no-specials", "--no-devices",
            "-e", shlex.join(ssh()[:-1]), "--"]


def fetch(job_id, results=RESULTS):
    if not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[a-f0-9]{10}", job_id):
        raise ValueError("実行ログに表示されたジョブIDを指定してください。")
    local = results / job_id
    local.mkdir(mode=0o700, parents=True, exist_ok=True)
    cmd = transfer()
    cmd.insert(1, "--ignore-existing")
    subprocess.run([*cmd, f"{HOST}:/workspace/pc-jobs/{job_id}/", str(local) + "/"], check=True)
    print(f"結果をこのPCに保存しました   {local}", file=sys.stderr, flush=True)


def job(path, args=(), *, results=RESULTS):
    path = Path(path).resolve(strict=True)
    if not path.is_file() or path.suffix.lower() != ".py":
        raise ValueError("実行する .py ファイルを指定してください。")
    job_id = datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:10]
    remote = "/workspace/pc-jobs/" + job_id
    local = results / job_id
    local.mkdir(mode=0o700, parents=True)
    cmd = ssh()
    sync = transfer()
    print(f"Podの作業先   {remote}\nPCの結果先    {local}", file=sys.stderr, flush=True)
    run([*cmd, f"umask 077 && mkdir -p /workspace/pc-jobs && mkdir {remote}"])
    subprocess.run([*sync, str(path), f"{HOST}:{remote}/"], check=True)
    script = shlex.join(["python3", "-u", "./" + path.name, *args])
    result = subprocess.run([*cmd, f"cd {remote} && exec {script}"])
    try:
        fetch(job_id, results)
    except (OSError, subprocess.SubprocessError):
        print(f"結果の再取得   runpod-python --fetch {job_id}", file=sys.stderr, flush=True)
        raise
    return result.returncode


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print("使い方  runpod-python スクリプト.py [引数 ...]\n指定した1ファイルをPodにコピーして実行し、実行フォルダーをこのPCに保存します。")
        sys.exit(0 if len(sys.argv) > 1 else 2)
    try:
        if sys.argv[1] == "--fetch" and len(sys.argv) == 3:
            fetch(sys.argv[2])
        else:
            sys.exit(job(sys.argv[1], sys.argv[2:]))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
