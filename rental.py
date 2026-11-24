"""Live RunPod offers and a durable, single-reservation worker."""
from contextlib import contextmanager
from decimal import Decimal, ROUND_CEILING
import fcntl
import hashlib
import hmac
import json
import os
import re
import ipaddress
from pathlib import Path
import secrets
import shlex
import subprocess
import sys
import time
import uuid
from urllib.parse import urlencode
import gpu
import inventory
from inventory import DISK, Offer, catalog

ROOT = gpu.PROFILE
ORDER = ROOT / "reservation.json"
PREFERRED = "NVIDIA RTX A6000"
PREFERRED_LIMIT = Decimal("0.34")
IMAGE = "runpod/pytorch:2.1.0-cu1281-torch280-ubuntu2404"
TERMINAL = {"ready", "failed", "cancelled", "stopped"}
def read_order():
    if gpu.connection().get("signed_out"):
        return {}
    return json.loads(ORDER.read_text()) if ORDER.exists() else {}


@contextmanager
def locked():
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (ROOT / "reservation.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def save(order, state, message):
    order.update(state=state, message=message, updated=time.time())
    gpu.atomic_write(ORDER, json.dumps(order, ensure_ascii=False))
    marker = ROOT / "reservation.pending"
    if state in TERMINAL:
        marker.unlink(missing_ok=True)
    elif not marker.exists():
        marker.touch(mode=0o600)


def reserve(offer, budget=PREFERRED_LIMIT, *, allow_jupyter=False, avoid_runtime_ip=None):
    with locked():
        old = read_order()
        if old and old["state"] not in TERMINAL:
            raise RuntimeError("すでに受付中の予約があります。")
        limit = min(offer.hourly, budget)
        if offer.available is None or offer.hourly > limit:
            raise RuntimeError("現在の在庫または料金では予約できません。")
        if not gpu.connection().get("configured") or not gpu.load_key():
            raise ValueError("「RunPodに接続」から本人確認してください。")
        gpu.prepare_connection()
        order = dict(model=offer.model, title=offer.name, cloud=offer.cloud, limit=str(limit),
                     name="desktop-gpu-" + uuid.uuid4().hex[:16], at=time.time(), previous=gpu.POD)
        order["vram_gb"] = offer.memory
        if offer.memory >= inventory.MIN_VRAM_GB and (ROOT / "strata-3090ti.enabled").exists():
            order["strata_iq3_s"] = True
        if allow_jupyter:
            order["allow_jupyter"] = True
        if avoid_runtime_ip:
            order["avoid_runtime_ip"] = str(ipaddress.ip_address(avoid_runtime_ip))
        save(order, "waiting", "空き状況と料金を確認します。")
        start_worker(order)


def start_worker(order):
    try:
        subprocess.run(["systemctl", "--user", "start", "--no-block", "runpod-reservation.service"],
                       check=True, timeout=8, capture_output=True)
    except (OSError, subprocess.SubprocessError) as exc:
        save(order, "failed", "予約サービスを開始できませんでした。")
        raise RuntimeError(order["message"]) from exc


def restart(key, pod, previous):
    # The caller holds the reservation lock. The worker observes this start and never resends it.
    if previous.get("pod") == pod["id"]:
        order = {k: previous[k] for k in ("name", "pod", "model", "title", "cloud", "limit")}
        if "vram_gb" in previous:
            order["vram_gb"] = previous["vram_gb"]
        if previous.get("allow_jupyter"):
            order["allow_jupyter"] = True
        if previous.get("strata_iq3_s"):
            order["strata_iq3_s"] = True
    else:
        price = (Decimal(str(pod["cost"])) + DISK).quantize(Decimal(".01"), rounding=ROUND_CEILING)
        order = dict(name=pod["name"], pod=pod["id"], model=pod["gpu"]["id"],
                     title=gpu.MODEL, cloud=pod["cloud"], limit=str(price))
    if order["name"] != pod["name"] or not matches_order(pod, order):
        raise RuntimeError("GPU・料金・ディスクが予約条件と一致しないため、起動を中止しました。")
    order["restart_requested"] = time.time()
    save(order, "booting", "再起動後にSSHとGPU計算を確認します。")
    start_worker(order)
    try:
        return gpu.api(key, "/action", {"action": "start"})
    except gpu.ApiError as exc:
        if record_account_refusal(order, exc):
            raise RuntimeError(order["message"]) from exc
        if 400 <= exc.status < 500 and exc.status != 408:
            save(order, "failed", f"再起動できませんでした。{exc}")
        raise


def cancel():
    with locked():
        order = read_order()
        if order.get("state") != "waiting":
            raise RuntimeError("すでに発注済みです。Podの状態を確認してください。")
        save(order, "cancelled", "予約を取り消しました。")


def enable_jupyter(name):
    with locked():
        order = read_order()
        if order.get("name") != name or order.get("state") != "booting":
            raise RuntimeError("予約状態が変わりました。現在のPodを確認してください。")
        order["allow_jupyter"] = True
        save(order, "booting", "直接SSHを確認し、接続できない場合はJupyterでGPU計算を確認します。")


def adopt(order, pod):
    pod_id = pod["id"]
    if not re.fullmatch("[a-z0-9]{8,32}", pod_id):
        raise ValueError("Pod IDが不正です。")
    if gpu.POD != pod_id:
        gpu.atomic_write(ROOT / "active-host.conf", f"Host {gpu.HOST}\n    HostName pending.invalid\n")
        target = {"id": pod_id, "model": order["title"], "gpu_id": order["model"]}
        if "vram_gb" in order:
            target["vram_gb"] = order["vram_gb"]
        gpu.atomic_write(gpu.target_file, json.dumps(target))
        gpu.reload_target()


def instance_list(key):
    path = "/v2/pods"
    pods = []
    while True:
        page = gpu.rest(key, path)
        pods.extend(page["pods"])
        pagination = page.get("pagination", {})
        if not pagination.get("hasNextPage"):
            return pods
        path = "/v2/pods?" + urlencode({"cursor": pagination["nextCursor"]})


def billing_balance():
    data = gpu.rest(gpu.load_key(), "/v2/billing/balance")
    return data["billingMode"], Decimal(str(data["balanceAmount"]))


def billing_refusal(exc):
    if exc.status == 402:
        return True
    detail = exc.detail.casefold()
    return exc.status in (400, 403, 409, 422) and any(term in detail for term in (
        "insufficient funds", "insufficient balance", "insufficient credit", "not enough funds",
        "not enough credit", "not enough balance", "balance is too low", "balance too low",
        "not have enough funds", "not have enough credit", "add funds", "fund your account",
        "payment method", "payment required", "credit balance"))


def record_account_refusal(order, exc):
    if billing_refusal(exc):
        order["recovery"] = "billing"
        message = "RunPodが支払い条件を理由に起動を受け付けませんでした。支払い設定と残高を確認してから、GPUを選び直してください。"
    elif exc.status in (401, 403):
        order["recovery"] = "account"
        message = "RunPodが操作を許可しませんでした。接続設定でアカウントを確認し、必要ならPodsの読み書き権限があるキーで接続し直してください。"
    else:
        return False
    save(order, "failed", message)
    return True


def reconcile(key, order):
    matches = [p for p in instance_list(key) if p.get("name") == order["name"]]
    if len(matches) == 1:
        order["pod"] = matches[0]["id"]
        save(order, "booting", "Podを割り当てました。起動を確認しています。")
    else:
        save(order, "submitted", "発注結果をRunPodに照会中です。重複発注を防ぐため再送はしません。")


def create(key, order):
    limit = Decimal(order["limit"])
    offers = catalog(order["model"], order["cloud"], limit)
    selected = next((o for o in offers if (o.model, o.cloud) == (order["model"], order["cloud"])), None)
    if not selected or selected.available is not True or selected.hourly > limit:
        order["waiting_for_stock"] = True
        reason = "空き待ち"
        if selected and selected.hourly > limit:
            reason = f"料金が上限を超えています（現在 ${selected.hourly:.2f}/時）"
        save(order, "waiting", f"{reason}・上限 ${limit:.2f}/時で自動確認中")
        return
    order.pop("waiting_for_stock", None)
    other = [p for p in instance_list(key) if p.get("id") == order["previous"]
             or str(p.get("name", "")).startswith("desktop-gpu-")]
    if any(p.get("status") not in ("EXITED","TERMINATED") for p in other):
        save(order, "waiting", "使用中のPodが停止してから、新しいGPUを起動します。")
        return
    # A per-order pairing secret authenticates the new server's SSH host key.
    proof_dir = gpu.account_folder(ROOT) / "pairing"
    proof_dir.mkdir(exist_ok=True, mode=0o700)
    proof = secrets.token_hex(32)
    gpu.atomic_write(proof_dir / order["name"], proof)
    public = gpu.IDENTITY.with_suffix(".pub").read_text().strip()
    payload = {"name":order["name"], "image":IMAGE, "disk":50, "cloud":order["cloud"],
               "gpu":{"id":order["model"],"count":1,"minCudaVersion":"12.8"},
               "ports":["22/tcp","8888/http"], "mounts":{}, "startJupyter":True, "startSsh":True,
               "env":{"PUBLIC_KEY":public,"RUNPOD_DESKTOP_PAIRING":proof}}
    order["vram_gb"] = selected.memory
    strata_terms = (selected.memory >= inventory.MIN_VRAM_GB and not order.get("strata_relaxed")
                    and (order.get("strata_iq3_s") or (ROOT / "strata-3090ti.enabled").exists()))
    if strata_terms:
        payload["gpu"].update(minRamPerGpu=62, minCudaVersion="13.0")
    save(order, "submitted", "RunPodに発注しています。")
    try:
        pod = gpu.rest(key, "/v2/pods", payload)
    except gpu.ApiError as exc:
        detail = exc.detail.lower()
        if record_account_refusal(order, exc):
            return
        if strata_terms and exc.status in (400, 404, 409, 422):
            # The stock list does not filter by these terms, so a listed GPU can lack a host with
            # 62 GB RAM and CUDA 13. RunPod refused before creating anything: order it once more without them.
            order["strata_relaxed"] = True
            save(order, "waiting", "Strata用の条件（RAM 62 GB・CUDA 13）に合うマシンがないため、通常の条件で発注し直します。")
            create(key, order)
            return
        if exc.status in (400, 409, 500, 503) and any(t in detail for t in
                ("no capacity","no longer available","no available","insufficient capacity","out of capacity","no instances","not enough gpu","no longer any gpu","no longer have any instances","no machine available")):
            save(order, "waiting", "在庫が変わりました。次回の更新で再確認します。")
        elif exc.status == 429:
            save(order, "waiting", "RunPodの利用制限が解除されるまで待機します。")
        elif 400 <= exc.status < 500 and exc.status != 408:
            save(order, "failed", f"発注できませんでした。{exc}")
        else:
            save(order, "submitted", "発注結果を照会しています。")
        return
    if pod.get("id"):
        order["pod"] = pod["id"]
        save(order, "booting", "Podの起動を待っています。")
        if all(field in pod for field in ("gpu", "cloud", "cost")) and not matches_order(pod, order):
            adopt(order, pod)
            stop_mismatch(key, order)


def matches_order(pod, order):
    price = pod.get("cost")
    mounts = pod.get("mounts") or {}
    return (price is not None and Decimal(str(price)) + DISK <= Decimal(order["limit"])
            and (pod.get("gpu") or {}).get("id") == order["model"]
            and (pod.get("gpu") or {}).get("count", 1) == 1
            and pod.get("cloud") == order["cloud"] and pod.get("disk", 50) == 50
            and not any(mounts.values()))


def stop_mismatch(key, order):
    order["reason"] = "割当条件が予約と一致しないため自動停止します。"
    save(order, "stopping", order["reason"])
    stop_allocated(key, order)


def stop_allocated(key, order):
    pod = gpu.rest(key, "/v2/pods/" + order["pod"])
    if pod.get("status") in ("EXITED","TERMINATED"):
        save(order, "stopped", order["reason"] + " 停止を確認しました。")
    elif "stop" in pod.get("actions", []):
        gpu.rest(key, "/v2/pods/" + order["pod"] + "/action", {"action":"stop"})
        save(order, "stopping", order["reason"] + " 停止を確認中です。")


class PairingError(RuntimeError):
    """A connection stage that is safe to show in the reservation UI."""


def pair(pod, order):
    direct = (pod.get("ssh") or {}).get("direct") or {}
    if not direct.get("host") or not direct.get("port"):
        ports = (pod.get("runtime") or {}).get("ports") or []
        if ports:
            raise PairingError("このPodには直接SSH用の公開アドレス・ポートがまだありません。")
        raise PairingError("RunPodのSSH接続先を待っています。")
    ip = str(ipaddress.ip_address(direct["host"]))
    port = int(direct["port"])
    if not 0 < port < 65536:
        raise ValueError("SSHポートが未確定")
    try:
        raw = gpu.run(["ssh-keyscan","-T","5","-p",str(port),"-t","ed25519",ip])
    except (RuntimeError, subprocess.SubprocessError) as exc:
        raise PairingError("PodのSSHサーバーからの応答を待っています。") from exc
    records = [line.split() for line in raw.splitlines() if line and not line.startswith("#")]
    if len(records) != 1 or len(records[0]) != 3 or records[0][1] != "ssh-ed25519":
        raise PairingError("SSHサーバーのホスト公開鍵を確認できていません。")
    fields = records[0]
    line = f"{gpu.HOST} {fields[1]} {fields[2]}\n"
    digest = hashlib.sha256(line.encode()).hexdigest()
    store = gpu.account_folder(ROOT) / "pods" / pod["id"]
    store.mkdir(parents=True, exist_ok=True, mode=0o700)
    gpu.atomic_write(store / digest, line)
    challenge = secrets.token_hex(24)
    code = """import hashlib,hmac,os,pathlib,sys
key=os.environ['RUNPOD_DESKTOP_PAIRING']
host=pathlib.Path('/etc/ssh/ssh_host_ed25519_key.pub').read_text().split()[1]
print(hmac.new(key.encode(),(sys.argv[1]+host).encode(),hashlib.sha256).hexdigest())"""
    command = 'if test -r /etc/rp_environment; then . /etc/rp_environment; fi; ' + shlex.join(["python3","-c",code,challenge])
    try:
        result = gpu.run(["ssh","-F","/dev/null","-oBatchMode=yes","-oIdentitiesOnly=yes",
                          "-oConnectTimeout=5","-oStrictHostKeyChecking=yes",
                          f"-oHostKeyAlias={gpu.HOST}",f"-oUserKnownHostsFile={store / digest}",
                          "-i",str(gpu.IDENTITY),"-p",str(port),"root@" + ip,command])
    except (RuntimeError, subprocess.SubprocessError) as exc:
        if "Permission denied" in str(exc):
            raise PairingError("このPCの公開鍵がPodで受け付けられていません。") from exc
        raise PairingError("SSH接続先が予約したPodであるか確認しています。") from exc
    secret = (gpu.account_folder(ROOT) / "pairing" / order["name"]).read_text()
    expected = hmac.new(secret.encode(), (challenge + fields[2]).encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, result):
        raise PairingError("予約したPodとSSHホスト鍵の照合が完了していません。")
    gpu.atomic_write(store / "endpoint.json", json.dumps({"ip":ip,"port":port,"pin":digest}))
    alias = (f"Host {gpu.HOST}\n    HostName {ip}\n    Port {port}\n    User root\n"
             f"    HostKeyAlias {gpu.HOST}\n    UserKnownHostsFile {store / digest}\n"
             f"    IdentityFile {gpu.IDENTITY}\n    IdentitiesOnly yes\n    StrictHostKeyChecking yes\n    ForwardAgent no\n")
    gpu.atomic_write(ROOT / "active-host.conf", alias)


def boot(key, order):
    pod = gpu.rest(key, "/v2/pods/" + order["pod"])
    adopt(order, pod)
    status = pod.get("status")
    if "restart_requested" in order:
        if status == "EXITED" and time.time() - order["restart_requested"] < 90:
            return
        if status != "EXITED":
            order.pop("restart_requested")
    if status in ("EXITED", "TERMINATED"):
        save(order, "stopped", "Podは停止しています。")
        return
    if not matches_order(pod, order):
        stop_mismatch(key, order)
        return
    if status == "RUNNING":
        ports = (pod.get("runtime") or {}).get("ports") or []
        if order.get("avoid_runtime_ip") and any(p.get("ip") == order["avoid_runtime_ip"] for p in ports):
            order["reason"] = "前回と同じ接続先が割り当てられたため、自動停止します。"
            save(order, "stopping", order["reason"])
            stop_allocated(key, order)
            return
        order.setdefault("running_since", time.time())
        stage = "SSH接続"
        direct = (pod.get("ssh") or {}).get("direct") or {}
        use_jupyter = order.get("allow_jupyter") and not (direct.get("port") and direct.get("host"))
        try:
            if not use_jupyter:
                try:
                    pair(pod, order)
                except PairingError:
                    if not order.get("allow_jupyter") or time.time() - order["running_since"] < 25:
                        raise
                    use_jupyter = True
            if use_jupyter:
                stage = "Jupyter接続とGPU計算"
                import jupyter_gpu
                try:
                    jupyter_gpu.check(pod)
                except jupyter_gpu.ComputeError as exc:
                    order["compute_failures"] = order.get("compute_failures", 0) + 1
                    if order["compute_failures"] >= 2:
                        order["reason"] = str(exc) + " 2回の実機確認に失敗したため、自動停止します。"
                        save(order, "stopping", order["reason"])
                        return
                    raise PairingError(str(exc) + " 再確認します。") from exc
                except Exception as exc:
                    raise PairingError("直接SSHが使えないため、Jupyterの起動・GPU計算を確認しています。") from exc
            else:
                stage = "PyTorchのGPU計算"
                gpu.gpu_test()
        except (RuntimeError, ValueError, OSError, ImportError, subprocess.SubprocessError) as exc:
            detail = str(exc) if isinstance(exc, PairingError) else f"{stage}を確認できていません。"
            if time.time() - order["running_since"] >= 240:
                order["reason"] = f"{detail} 4分以内に{stage}を確認できず、自動停止します。"
                save(order, "stopping", order["reason"])
                stop_allocated(key, order)
                return
            save(order, "booting", f"GPU起動済み。{detail}")
            return
        order["connection_mode"] = "jupyter" if use_jupyter else "ssh"
        message = ("JupyterでGPU計算を確認しました。直接SSHは未接続です。" if use_jupyter
                   else "SSH接続・PyTorchのGPU計算を確認しました。")
        save(order, "ready", message)
        try:
            subprocess.run(["gapplication", "launch", "jp.local.RunpodDesktop"], check=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"作業画面の自動表示を開始できませんでした: {exc}", file=sys.stderr)
    elif status == "ERROR":
        if "stop" not in pod.get("actions", []):
            save(order, "failed", "RunPodが起動エラーを返しました。「Podを操作」で状態を確認してください。")
            return
        order["reason"] = "RunPodの起動エラーです。"
        save(order, "stopping", order["reason"])


def tick():
    with locked():
        order = read_order()
        state = order.get("state")
        if not state or state in TERMINAL:
            return False
        try:
            key = gpu.load_key()
            if state == "waiting":
                create(key, order)
            elif state == "submitted":
                reconcile(key, order)
            elif state == "booting":
                boot(key, order)
            elif state == "stopping":
                stop_allocated(key, order)
        except Exception as exc:
            # The persisted state, especially a submitted POST, determines the next safe step.
            order = read_order()
            if order["state"] == "waiting" and isinstance(exc, gpu.ApiError) and record_account_refusal(order, exc):
                return False
            order.pop("waiting_for_stock", None)
            save(order, order["state"], f"再確認待ち  {str(exc)[:230]}")
        return read_order().get("state") not in TERMINAL


def wait_for_next_check(order):
    if order.get("state") != "waiting":
        time.sleep(8)
        return
    deadline = time.monotonic() + inventory.NORMAL_INTERVAL
    stock_file = inventory.ROOT / "inventory.json"
    last_stamp = None
    while time.monotonic() < deadline:
        current = read_order()
        if current.get("name") != order["name"] or current.get("state") != "waiting":
            return
        if current.get("waiting_for_stock"):
            try:
                stamp = stock_file.stat().st_mtime_ns
            except FileNotFoundError:
                stamp = None
            if stamp != last_stamp:
                last_stamp = stamp
                stock = inventory.read_snapshot()
                if stock.is_fresh(time.time()) and any(
                    v.model == order["model"] and v.cloud == order["cloud"] and v.available is True
                    and v.hourly <= Decimal(order["limit"]) for v in stock.offers
                ):
                    return
        time.sleep(min(1, max(0, deadline - time.monotonic())))


if __name__ == "__main__":
    while True:
        previous_name = read_order().get("name")
        if not tick():
            break
        order = read_order()
        if order.get("name") == previous_name and order.get("state") not in TERMINAL:
            wait_for_next_check(order)
