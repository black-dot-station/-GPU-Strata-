"""Stop this app's GPU Pods once they have stayed idle, so that no paid minute is wasted.

A systemd user timer runs this once a minute, whether or not the window is open.
"""
from contextlib import suppress
import datetime
import fcntl
import json
import shutil
import subprocess
import sys
import time

import gpu
import rental

SETTINGS = gpu.PROFILE / "idle-stop.json"
STATE = gpu.PROFILE / "idle-state.json"
HOLD = gpu.PROFILE / "idle-hold"
LOG = gpu.PROFILE / "idle-stop.log"
CHOICES = (2, 5, 10, 15, 30)
DEFAULT_MINUTES = 5
GPU_IDLE = 2          # percent; at or below this the GPU counts as unused
CPU_IDLE = 5          # percent; CPU-only work such as downloads keeps the Pod
HOLD_SECONDS = 90     # the desktop app renews the hold while it is preparing or running work
START_GRACE = 180     # seconds after the container starts


def settings():
    try:
        data = json.loads(SETTINGS.read_text())
    except (OSError, ValueError):
        data = {}
    minutes = data.get("minutes", DEFAULT_MINUTES)
    return bool(data.get("enabled", True)), minutes if minutes in CHOICES else DEFAULT_MINUTES


def save_settings(enabled, minutes):
    gpu.PROFILE.mkdir(mode=0o700, parents=True, exist_ok=True)
    gpu.atomic_write(SETTINGS, json.dumps({"enabled": bool(enabled), "minutes": minutes}))
    if not enabled:
        STATE.unlink(missing_ok=True)


def read_state():
    try:
        data = json.loads(STATE.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def hold():
    """Called by the desktop app while it is doing work that may not show up as GPU use."""
    gpu.PROFILE.mkdir(mode=0o700, parents=True, exist_ok=True)
    HOLD.touch(mode=0o600)


def held(now):
    try:
        return now - HOLD.stat().st_mtime < HOLD_SECONDS
    except FileNotFoundError:
        return False


def ensure_timer():
    subprocess.run(["systemctl", "--user", "enable", "--now", "runpod-idle.timer"],
                   check=True, capture_output=True, timeout=15)


def usage(pod):
    """GPU and CPU utilisation in percent. None when RunPod reports nothing usable."""
    runtime = pod.get("runtime") or {}
    gpus = [g.get("util") for g in runtime.get("gpus") or []]
    cpu = (runtime.get("cpu") or {}).get("util")
    if gpus and all(isinstance(u, (int, float)) for u in gpus):
        return max(gpus), cpu if isinstance(cpu, (int, float)) else None
    if pod.get("id") == gpu.POD:
        with suppress(OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            return gpu.telemetry()[1], None
    return None, None


def is_idle(pod):
    gpu_use, cpu_use = usage(pod)
    if gpu_use is None:
        return False  # never stop a Pod on missing data
    return gpu_use <= GPU_IDLE and (cpu_use is None or cpu_use <= CPU_IDLE)


def log(message):
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with suppress(OSError):
        lines = LOG.read_text().splitlines()[-199:] if LOG.exists() else []
        gpu.atomic_write(LOG, "\n".join(lines + [f"{stamp}  {message}"]) + "\n")


def notify(message):
    if shutil.which("notify-send"):
        with suppress(OSError, subprocess.SubprocessError):
            subprocess.run(["notify-send", "--app-name=RunPod GPU マネージャー", "-i", "jp.local.RunpodDesktop",
                            "GPUを自動停止しました", message], timeout=5, capture_output=True)


def tick(now=None):
    now = time.time() if now is None else now
    enabled, minutes = settings()
    if not enabled or gpu.connection().get("signed_out"):
        STATE.unlink(missing_ok=True)
        return
    key = gpu.load_key()
    if not key:
        return
    gpu.reload_target()
    order = rental.read_order()
    previous = read_state()
    current = {}
    for listed in rental.instance_list(key):
        pod_id = listed.get("id")
        if listed.get("status") != "RUNNING" or not (pod_id == gpu.POD or str(listed.get("name", "")).startswith("desktop-gpu-")):
            continue
        if order.get("pod") == pod_id and order.get("state") not in rental.TERMINAL:
            continue  # the reservation worker is still bringing this Pod up
        pod = gpu.rest(key, "/v2/pods/" + pod_id)
        uptime = (pod.get("runtime") or {}).get("uptime") or 0
        if pod.get("status") != "RUNNING" or uptime < START_GRACE or held(now) or not is_idle(pod):
            continue
        since = (previous.get(pod_id) or {}).get("idle_since", now)
        if now - since < minutes * 60:
            current[pod_id] = {"idle_since": since, "stop_at": since + minutes * 60}
            continue
        if "stop" not in pod.get("actions", []):
            continue
        gpu.rest(key, f"/v2/pods/{pod_id}/action", {"action": "stop"})
        name = (pod.get("gpu") or {}).get("id", "GPU").removeprefix("NVIDIA ")
        message = f"{name}（Pod {pod_id}）を{minutes}分間使っていなかったため停止しました。"
        log(message)
        notify(message)
    gpu.atomic_write(STATE, json.dumps(current))


if __name__ == "__main__":
    gpu.PROFILE.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (gpu.PROFILE / "idle.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit(0)
        try:
            tick()
        except (OSError, ValueError, RuntimeError, KeyError) as exc:
            log(f"確認できませんでした: {str(exc)[:200]}")
            sys.exit(1)
