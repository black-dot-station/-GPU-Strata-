"""Reusable one-button upstream Strata installation, local weight verification, and update checks."""
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import urllib.request
from gi.repository import GLib, Gio

import gpu
import strata_bridge as bridge
from strata_pod import CHECKPOINT, MODEL, MODEL_URL, SHARDS, RELEASE, digest, ranged_fetch

APP_NAME = 'Strata-0.2.19-linux-x86_64.AppImage'
APP_DIGEST = 'aac1935c8f2ee179c21e52249fbb8bd0ef9b45141a8054094abaa6dfbdb34d32'
LOCAL = Path(GLib.get_user_data_dir()) / 'runpod-desktop'
MANIFEST = gpu.PROFILE/'strata-model.json'


def home_app(notify):
    try:
        return bridge.app_port()
    except (OSError, RuntimeError, ValueError, KeyError):
        pass
    apps = {app.get_id(): app for app in Gio.AppInfo.get_all()}
    for desktop_id in ('strata-upstream.desktop', 'Strata.desktop', 'ai.strata.desktop.desktop'):
        if desktop_id in apps:
            apps[desktop_id].launch([], None)
            return wait_for_home_app()
    folder = LOCAL/'upstream'
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    app = folder / APP_NAME
    if not app.exists() or digest(app) != APP_DIGEST:
        notify('本家Strataをダウンロードしています。')
        with tempfile.NamedTemporaryFile(dir=folder, delete=False) as temp:
            pending = Path(temp.name)
        try:
            subprocess.run(['curl','-fsSL','--retry','3','-o',str(pending),RELEASE+APP_NAME],check=True)
            if digest(pending) != APP_DIGEST:
                raise ValueError('本家Strataの照合に失敗しました。')
            pending.chmod(0o700)
            pending.replace(app)
        finally:
            pending.unlink(missing_ok=True)
    # Only this program's verified AppImage is invoked. A separately installed official app takes priority.
    subprocess.Popen([str(app)], stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
    return wait_for_home_app()


def wait_for_home_app():
    for _ in range(50):
        time.sleep(.4)
        try:
            return bridge.app_port()
        except (OSError, RuntimeError, ValueError, KeyError):
            pass
    raise RuntimeError('本家Strataの起動を確認できませんでした。')


def metadata(path):
    info = path.stat()
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]


def source_paths():
    if not MANIFEST.exists():
        return []
    try:
        rows = json.loads(MANIFEST.read_text())['files']
        if len(rows) != len(SHARDS):
            return []
        paths = []
        for row, (size, sha) in zip(rows, SHARDS):
            path = Path(row['path'])
            if row.get('sha256') != sha or metadata(path) != row['stat'] or row['stat'][2] != size:
                return []
            paths.append(path)
        return paths
    except (KeyError, ValueError, OSError):
        return []


def setup_local(notify):
    """Safe to repeat. Reuse matching weights already saved by either official or older installations."""
    gpu.PROFILE.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (gpu.PROFILE/'strata-home.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('AIセットアップはすでに進行中です。')
        notify('本家Strataを確認しています。')
        host = home_app(notify)
        if source_paths():
            return
        info = bridge.request(host, '/api/native', timeout=25)
        installed = info.get('installed') or {}
        dest = Path(info.get('downloadDirectory') or LOCAL/'models') / 'IQ3_S'
        # Use the upstream library that holds the active installation and its model files.
        if installed.get('data_folder'):
            dest = Path(installed['data_folder'])/'Models/IQ3_S'
        extra = [dest, LOCAL/'models/IQ3_S', Path.home()/'.local/share/xestrata/models/IQ3_XXS']
        dest.mkdir(mode=0o700, exist_ok=True, parents=True)
        srcs = []
        for i, (size, sha) in enumerate(SHARDS, 1):
            name = f'{CHECKPOINT}-{i:05}-of-00002.gguf'
            candidates = [dest/name]
            for directory in extra:
                if directory.is_dir():
                    candidates.extend(directory.glob(f'*-{i:05}-of-00002.gguf'))
            path = None
            for candidate in dict.fromkeys(candidates):
                if candidate.is_file() and candidate.stat().st_size == size:
                    notify(f'自宅SSDのモデル {i}/2 を照合しています。')
                    if digest(candidate) == sha:
                        path = candidate
                        break
            if path is None:
                path = dest/name
                if path.exists():
                    raise ValueError(f'既存のモデル {i}/2 が公式版と一致しません。保存先を確認してください。')
                if shutil.disk_usage(dest).free <= size + (1 << 30):
                    raise ValueError('自宅SSDの空き容量が不足しています。')
                with tempfile.NamedTemporaryFile(dir=dest,delete=False) as tmp:
                    pending = Path(tmp.name)
                    try:
                        ranged_fetch(MODEL_URL+name,tmp.fileno(),size,f'自宅SSDにQwenを保存中 {i}/2',notify)
                        if digest(pending) != sha:
                            raise ValueError('ダウンロードしたモデルの照合に失敗しました。')
                        pending.replace(path)
                    finally:
                        pending.unlink(missing_ok=True)
            srcs.append({'path':str(path), 'sha256':sha, 'stat':metadata(path)})
        gpu.atomic_write(MANIFEST,json.dumps({'files':srcs,'model':MODEL}, ensure_ascii=False))


def check_updates():
    """Ask upstream's signed release feed, and compare both weight hashes to the model publisher."""
    result = bridge.request(bridge.app_port(), '/api/updates/check',timeout=30)
    app, engine = result['strata'], result['engine']
    def versions(row):
        current, latest = row.get('installedVersion'), row.get('latestVersion')
        if row.get('status') == 'available' and current and latest:
            return f'{current} → {latest}'
        if current and current == latest:
            return f'{current}（最新）'
        return '確認できませんでした'
    url = 'https://huggingface.co/api/models/ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-GGUF/tree/main/IQ3_S'
    try:
        with urllib.request.urlopen(url,timeout=12) as response:
            files = {Path(x['path']).name:x.get('lfs',{}).get('oid') for x in json.load(response)}
        hashes = [files[f'{CHECKPOINT}-{i:05}-of-00002.gguf'] for i in (1,2)]
        model = '公式ファイルに変更なし' if hashes == [sha for _,sha in SHARDS] else '新しい公式ファイルあり'
    except (OSError, ValueError, KeyError):
        model = '確認できませんでした'
    return f'本家Strata {versions(app)}　／　エンジン {versions(engine)}　／　Qwen IQ3_S {model}'


def inference_endpoint():
    """Return the OpenAI-compatible endpoint and model while this manager's verified GPU is available."""
    target = gpu.target_settings().get('id')
    try:
        ready = json.loads((gpu.PROFILE/'strata-ready.json').read_text())
    except (OSError, ValueError) as exc:
        raise ConnectionError('GPUマネージャーで3090 TiのQwenを接続してください。') from exc
    if not target or ready.get('pod') != target or ready.get('model') != MODEL:
        raise ConnectionError('3090 TiのQwenに接続されていません。')
    port = int(ready['port'])
    if MODEL not in [row.get('id') for row in bridge.request(port, '/v1/models', timeout=3).get('data',[])]:
        raise ConnectionError('3090 TiのQwenが応答していません。')
    return f'http://127.0.0.1:{port}/v1', MODEL
