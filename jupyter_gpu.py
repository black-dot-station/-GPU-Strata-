"""Check CUDA in a temporary kernel on the selected Pod's authenticated Jupyter server."""
import datetime
import http.client
import json
import re
import time
import uuid

import websocket
import gpu


class ComputeError(RuntimeError):
    pass


def check(pod):
    pod_id = pod["id"]
    if not re.fullmatch(r"[a-z0-9]{8,32}", pod_id) or "8888/http" not in (pod.get("ports") or []):
        raise ValueError("Jupyter接続先がありません。")
    host = f"{pod_id}-8888.proxy.runpod.net"
    env = pod.get("env") or {}
    token = env.get("JUPYTER_PASSWORD") or env.get("JUPYTER_TOKEN")
    if not token:
        raise ValueError("Jupyterの認証情報を待っています。")
    auth = {"Authorization": "token " + token}

    def api(method, path, body=None):
        conn = http.client.HTTPSConnection(host, timeout=8)
        try:
            data = json.dumps(body).encode() if body is not None else None
            conn.request(method, path, data, dict(auth, **{"Content-Type": "application/json"}))
            response = conn.getresponse()
            if response.status not in (200, 201, 204):
                raise ConnectionError(f"Jupyter HTTP {response.status}")
            raw = response.read(65536)
            return json.loads(raw) if raw else None
        finally:
            conn.close()

    kernel = api("POST", "/api/kernels", {"name": "python3"})["id"]
    if not re.fullmatch(r"[a-zA-Z0-9-]{1,80}", kernel):
        raise ValueError("JupyterカーネルIDが無効です。")
    path = "/api/kernels/" + kernel
    try:
        session = uuid.uuid4().hex
        url = f"wss://{host}{path}/channels?session_id={session}"
        ws = websocket.create_connection(url, header=auth, origin="https://" + host, timeout=8, redirect_limit=0)
        try:
            if ws.getstatus() != 101:
                raise ConnectionError("Jupyterチャネルを確認できません。")
            run_test(ws, session)
        finally:
            ws.close(timeout=2)
    finally:
        api("DELETE", path)


def run_test(ws, session):
    msg_id = uuid.uuid4().hex
    marker = "gpu-ok-" + msg_id
    header = dict(msg_id=msg_id, username="gpu-desktop", session=session, msg_type="execute_request",
                  version="5.3", date=datetime.datetime.now(datetime.timezone.utc).isoformat())
    content = dict(code=gpu.TEST + f"\nprint({marker!r}, flush=True)", silent=False, store_history=False,
                   user_expressions={}, allow_stdin=False, stop_on_error=True)
    ws.send(json.dumps(dict(channel="shell", header=header, parent_header={}, metadata={}, content=content, buffers=[])))
    deadline = time.monotonic() + 40
    replied = False
    stdout = ""
    while time.monotonic() < deadline:
        try:
            message = json.loads(ws.recv())
        except websocket.WebSocketTimeoutException:
            continue
        if message.get("parent_header", {}).get("msg_id") != msg_id:
            continue
        kind = message["header"]["msg_type"]
        data = message["content"]
        if kind == "error" or (kind == "execute_reply" and data.get("status") != "ok"):
            if "cuda unknown error" in str(data.get("evalue", "")).lower():
                raise ComputeError("Jupyterには接続できましたが、CUDAの初期化に失敗しました。")
            raise ComputeError("Jupyterには接続できましたが、PyTorchのGPU計算に失敗しました。")
        if kind == "stream" and data.get("name") == "stdout":
            stdout = (stdout + data.get("text", ""))[-4096:]
        if kind == "execute_reply":
            replied = True
        if replied and marker in stdout.splitlines():
            return
    raise TimeoutError("JupyterのGPU計算の応答を待っています。")
