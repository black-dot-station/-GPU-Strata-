"""Connect the official local Strata App Host to this user's selected GPU."""
from datetime import datetime, timezone
import http.client
import json
from pathlib import Path
import socket
import subprocess
import time
import threading
from uuid import uuid4

import gpu
from strata_pod import MODEL, PORT

OPTION = gpu.PROFILE / 'strata-3090ti.enabled'
WORK = '/workspace/.runpod-strata'


def request(port, path, body=None, *, method=None, timeout=10):
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=timeout)
    try:
        conn.request(method or ('POST' if body is not None else 'GET'), path,
                     json.dumps(body).encode() if body is not None else None,
                     {'Content-Type':'application/json', 'X-Strata-Client':'runpod-desktop'})
        res = conn.getresponse()
        data = res.read()
        if res.status >= 300:
            raise RuntimeError(f'Strata HTTP {res.status}')
        return json.loads(data)
    finally:
        conn.close()


def app_port():
    session = json.loads((Path.home()/'.strata/app-session.json').read_text())
    port = int(session['port'])
    info = request(port, '/api/health', timeout=2)
    if not (info.get('ok') and 'backend' in info and 'engine' in info):
        raise ValueError('本家Strataの応答を確認できませんでした。')
    return port


def prepare(ssh, source_port=None):
    # The worker holds the lock throughout downloading, packing and serving. A reopened window can reattach.
    cmd = (f'umask 077 && mkdir -p {WORK} && cd {WORK} && '
           'f=$(mktemp ./worker.XXXXXX) && cat > "$f" && mv "$f" worker.py && '
           f'{{ RUNPOD_STRATA_SOURCE_PORT={int(source_port or 0)} nohup python3 -u worker.py </dev/null >>worker.log 2>&1 & }}')
    code = Path(__file__).with_name('strata_pod.py').read_bytes()
    subprocess.run(ssh + [cmd], input=code, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=18, check=True)


def progress(ssh):
    check = f'cd {WORK} && cat status.json && if flock -n worker.lock true; then exit 3; fi'
    result = subprocess.run(ssh + [check], capture_output=True, timeout=12)
    if result.returncode == 255:
        raise ConnectionError('3090 TiへのSSH接続を待っています。')
    if result.returncode not in (0, 3):
        return {'stage':'preparing','message':'公式エンジンを準備しています。'}
    state = json.loads(result.stdout)
    if result.returncode == 3 and state['stage'] != 'failed':
        return {'stage':'failed','message':'Qwenの準備処理が終了しました。再接続できます。'}
    return state


def pick_port(folder):
    saved = folder / 'strata-port'
    if saved.is_file():
        port = int(saved.read_text())
        if not 1024 <= port <= 65535:
            raise ValueError('Strataの接続ポートが正しくありません。')
        return port
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    gpu.atomic_write(saved, str(port))
    return port


def forward(ssh, port):
    return subprocess.Popen([*ssh[:-1], '-oExitOnForwardFailure=yes', '-N',
                             '-L', f'127.0.0.1:{port}:127.0.0.1:{PORT}', ssh[-1]],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def verify_answer(port):
    listing = request(port, '/v1/models', timeout=5)
    if MODEL not in [m.get('id') for m in listing.get('data', [])]:
        raise ValueError('Qwen IQ3_Sを読み込み中です。')
    reply = request(port, '/v1/chat/completions', {'model':MODEL, 'messages':[{'role':'user','content':'2 + 2 = ? 数字だけで答えて。'}],
                    'max_tokens':48, 'temperature':0, 'reasoning_effort':'none'}, timeout=120)
    content = reply['choices'][0]['message']['content']
    if not isinstance(content, str) or content.strip().strip('.。').strip().replace('４', '4') != '4':
        raise ValueError('Qwenの計算テストを確認できませんでした。')


def chat_route(host, port, pod, folder):
    name = 'RunPod 3090 Ti · ' + pod
    base = f'http://127.0.0.1:{port}/v1'
    rows = request(host, '/api/model-providers')['connections']
    saved = next((item for item in rows if item['name']==name and item['baseUrl']==base and item['protocol']=='openai-chat'), None)
    if not saved:
        body = {'kind':'custom', 'name':name, 'baseUrl':base, 'protocol':'openai-chat',
                'models':[{'id':MODEL,'name':'Qwen3.8-Flash-Next IQ3_S · 3090 Ti','contextMax':8192,'vision':False}]}
        rows = request(host, '/api/model-providers', body)['connections']
        saved = next(item for item in rows if item['name']==name and item['baseUrl']==base)
    choice = {'connectionId':saved['id'], 'modelId':MODEL}
    chatfile = folder / 'strata-chat'
    chat_id = chatfile.read_text().strip() if chatfile.exists() else ''
    ids = {c['id'] for c in request(host,'/api/chats')['chats']}
    if chat_id in ids:
        session = request(host, '/api/chats/'+chat_id)['session']
        if session.get('preferences',{}).get('remoteModel') == choice:
            return f'http://127.0.0.1:{host}/#/chat/{chat_id}'
    chat_id = 's-runpod-' + uuid4().hex
    stamp = datetime.now(timezone.utc).isoformat()
    workspaces = request(host, '/api/workspaces')['workspaces']
    chat = {'id':chat_id,'title':'RTX 3090 Ti · Qwen IQ3_S','createdAt':stamp,'updatedAt':stamp,
            'messages':[],'tokensIn':0,'tokensOut':0,'preferences':{'mode':'manual','remoteModel':choice}}
    if workspaces:
        chat['workspace'] = workspaces[0]['path']
    request(host, '/api/chats/'+chat_id, chat, method='PUT')
    gpu.atomic_write(chatfile, chat_id)
    return f'http://127.0.0.1:{host}/#/chat/{chat_id}'


class ConnectionStop(threading.Event):
    def __init__(self):
        super().__init__()
        self.guard = threading.Lock()
        self.processes = []

    def attach(self, process):
        with self.guard:
            self.processes = [p for p in self.processes if p.poll() is None]
            self.processes.append(process)
            if self.is_set() and process.poll() is None:
                process.terminate()

    def set(self):
        with self.guard:
            super().set()
            for process in self.processes:
                if process.poll() is None:
                    process.terminate()


def connect(pod, ssh, folder, stop, notify):
    """Only the worker thread owns its tunnel. A Pod switch cancels callbacks and closes the forward."""
    tunnel = source = None
    instance = uuid4().hex
    try:
        if stop.is_set() or gpu.target_settings().get('id') != pod:
            return
        from strata_source import ModelSource
        from strata_setup import source_paths, home_app
        source = ModelSource(source_paths(), ssh)
        if source.tunnel is not None:
            stop.attach(source.tunnel)
        if stop.is_set() or gpu.target_settings().get('id') != pod:
            return
        prepare(ssh, source.port)
        port = pick_port(folder)
        tested = False
        while not stop.is_set() and gpu.target_settings().get('id') == pod:
            try:
                state = progress(ssh)
                if state['stage'] == 'failed':
                    notify('failed', state['message'])
                    return
                if state['stage'] not in ('ready','starting'):
                    notify('preparing', state['message'])
                else:
                    if source is not None:
                        source.close()
                        source = None
                    if tunnel is None or tunnel.poll() is not None:
                        tunnel = forward(ssh, port)
                        stop.attach(tunnel)
                        tested = False
                        if stop.wait(1):
                            break
                        if tunnel.poll() is not None:
                            raise ConnectionError('SSH転送を開始できませんでした。接続を再確認します。')
                    if not tested:
                        notify('preparing', '3090 TiでQwenの返答を確認しています。')
                        verify_answer(port)
                        if stop.is_set() or gpu.target_settings().get('id') != pod:
                            break
                        host = home_app(lambda msg: notify('preparing', msg))
                        if stop.is_set() or gpu.target_settings().get('id') != pod:
                            break
                        route = chat_route(host, port, pod, folder)
                        if stop.is_set() or gpu.target_settings().get('id') != pod:
                            break
                        tested = True
                        gpu.atomic_write(gpu.PROFILE/'strata-ready.json', json.dumps({'pod':pod,'model':MODEL,'port':port,'owner':instance}))
                        notify('ready', route)
                    else:
                        request(port, '/v1/models', timeout=6)
            except (OSError, RuntimeError, ValueError, KeyError, IndexError, subprocess.TimeoutExpired) as error:
                tested = False
                notify('preparing', f'本家Strataの接続を確認中　{error}')
            stop.wait(20 if tested else 8)
    except Exception as error:
        notify('failed', '3090 Tiに接続できませんでした。' + str(error)[:160])
    finally:
        if source is not None:
            source.close()
        readyfile = gpu.PROFILE/'strata-ready.json'
        try:
            if json.loads(readyfile.read_text()).get('owner') == instance:
                readyfile.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass
        if tunnel is not None and tunnel.poll() is None:
            tunnel.terminate()
            try:
                tunnel.wait(3)
            except subprocess.TimeoutExpired:
                tunnel.kill()
                tunnel.wait()
