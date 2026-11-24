"""Start the pinned upstream Strata Qwen engine inside the selected Linux GPU Pod."""
import concurrent.futures
import fcntl
import hashlib
import json
import re
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

ROOT = Path('/workspace/.runpod-strata')
MIN_VRAM_GB = 24
MIN_FREE_BYTES = 20 * 1024**3
PORT = 18697
MODEL = 'qwen3.8-flash-next-iq3_s'
CHECKPOINT = 'Qwen3.8-Flash-Next-GSQ-RCO-IQ3_S'
REVISION = 'ed59f92082b1e93c0e96d60a8b11aab089b52f09'
MODEL_URL = f'https://huggingface.co/ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-GGUF/resolve/{REVISION}/IQ3_S/'
SHARDS = [(54817524224, '4c1eb2ceb4915e1192f4f386021897bde56a97f40a0bb78bb86465e0f7d2aca3'),
          (28800138432, '316b46f3a2dbd68c900f43136ab9449f9dcc3725dfd8c794847c204bc161e113')]
RELEASE = 'https://github.com/Niko1221/Strata/releases/download/v0.2.19/'
ASSETS = [('control', 'strata-control-0.1.65-linux-x86_64.tar.gz', 'b44caf1980731edf2a0cdf9170cffdd8d240da87663016b6cb04f612582300d3'),
          ('engine', 'strata-engine-0.1.65-linux-x86_64-cuda-cuda13.tar.gz', '77a6fd933d41f1968c069fe04995f729128735d0fc7c53e446dd4a1f9c7fbf49')]


def status(message, stage='preparing'):
    path = ROOT / 'status.tmp'
    path.write_text(json.dumps({'stage': stage, 'message': message, 'updated': time.time()}, ensure_ascii=False))
    path.replace(ROOT / 'status.json')


def digest(file):
    with open(file, 'rb') as stream:
        h = hashlib.sha256()
        for block in iter(lambda: stream.read(8 << 20), b''):
            h.update(block)
        return h.hexdigest()


def install_asset(label, name, expected):
    dest = ROOT / label
    if (dest / '.release').is_file() and (dest / '.release').read_text() == expected:
        return
    with tempfile.TemporaryDirectory(dir=ROOT) as temp:
        archive = Path(temp) / name
        subprocess.run(['curl', '-fsSL', '--retry', '3', '--connect-timeout', '12', '-o', str(archive), RELEASE + name], check=True)
        if digest(archive) != expected:
            raise ValueError('本家Strataの配布ファイルの検証に失敗しました。')
        with tarfile.open(archive) as bundle:
            folder = Path(temp) / 'package'
            bundle.extractall(folder, filter='data')
        marker = 'setup.py' if label == 'control' else 'engine-component.json'
        root = next(folder.rglob(marker)).parent
        (root / '.release').write_text(expected)
        if dest.exists() or dest.is_symlink():
            # An older or unfinished release is left in the same workspace after an upgrade.
            # Move it into the temporary folder, which removes it on exit.
            dest.rename(Path(temp) / 'previous')
        root.rename(dest)


def ranged_fetch(url, fd, total, label, report=status, fallback=None):
    """Write independent verified ranges directly into the destination, with bounded RAM."""
    chunk = 64 << 20
    def copy(start):
        end = min(total, start + chunk) - 1
        for attempt in range(4):
            try:
                source = fallback if fallback and attempt >= 2 else url
                req = urllib.request.Request(source, headers={'Range': f'bytes={start}-{end}', 'Accept-Encoding': 'identity'})
                with urllib.request.urlopen(req, timeout=60) as response:
                    if response.status != 206 or response.headers.get('Content-Range') != f'bytes {start}-{end}/{total}':
                        raise IOError('モデルの配布元が指定した範囲を返しませんでした。')
                    pos = start
                    while pos <= end:
                        data = response.read(min(1 << 20, end + 1 - pos))
                        if not data:
                            raise IOError('モデルの転送が途中で終了しました。')
                        view = memoryview(data)
                        while view:
                            n = os.pwrite(fd, view, pos)
                            pos += n
                            view = view[n:]
                    return end + 1 - start
            except (OSError, ValueError):
                if attempt == 3:
                    raise
                time.sleep(2 ** attempt)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        jobs = [pool.submit(copy, i) for i in range(0, total, chunk)]
        count = 0
        last = 0
        try:
            for job in concurrent.futures.as_completed(jobs):
                count += job.result()
                if time.monotonic() - last > 3:
                    report(f'{label}　{100 * count // total}%')
                    last = time.monotonic()
        finally:
            for job in jobs:
                job.cancel()


def select_device(devices):
    for index, name, total, free, capability in devices:
        if total >= MIN_VRAM_GB * 10**9 and free is not None and free >= MIN_FREE_BYTES and capability >= (7, 5):
            return index, name
    summary = '、'.join(f'{name}（合計{total / 10**9:.1f} GB' +
                       (f'・空き{free / 1024**3:.1f} GiB）' if free is not None else '）')
                       for _, name, total, free, _ in devices)
    raise ValueError('NVIDIA GPU 1枚にVRAM 24 GB以上・空き20 GiB以上・Compute Capability 7.5以上が必要です。' + summary)


def probe_device():
    # The CUDA driver enumerates only devices visible to this process, including assigned MIG slices.
    import ctypes as ct
    try:
        lib = ct.CDLL('libcuda.so.1')
    except OSError as exc:
        raise ValueError('PodでNVIDIA CUDAドライバーを利用できません。') from exc
    def call(name, *args):
        result = getattr(lib, name)(*args)
        if result:
            raise RuntimeError(f'PodのCUDAドライバーを確認できません（{name}, {result}）。')
    call('cuInit', 0)
    version, count = ct.c_int(), ct.c_int()
    call('cuDriverGetVersion', ct.byref(version))
    if version.value < 13000:
        raise ValueError('公式エンジンにはCUDA 13対応のNVIDIAドライバーが必要です。')
    call('cuDeviceGetCount', ct.byref(count))
    devices = []
    for index in range(count.value):
        device, major, minor = ct.c_int(), ct.c_int(), ct.c_int()
        name = ct.create_string_buffer(256)
        total, free = ct.c_size_t(), ct.c_size_t()
        call('cuDeviceGet', ct.byref(device), index)
        call('cuDeviceGetName', name, len(name), device)
        call('cuDeviceComputeCapability', ct.byref(major), ct.byref(minor), device)
        call('cuDeviceTotalMem_v2', ct.byref(total), device)
        available = None
        if total.value >= MIN_VRAM_GB * 10**9 and (major.value, minor.value) >= (7, 5):
            context = ct.c_void_p()
            call('cuDevicePrimaryCtxRetain', ct.byref(context), device)
            try:
                call('cuCtxSetCurrent', context)
                usable = ct.c_size_t()
                call('cuMemGetInfo_v2', ct.byref(free), ct.byref(usable))
                available = free.value
            finally:
                call('cuCtxSetCurrent', None)
                call('cuDevicePrimaryCtxRelease_v2', device)
        devices.append((index, name.value.decode(errors='replace'), total.value, available, (major.value, minor.value)))
        if available is not None and available >= MIN_FREE_BYTES:
            break
    return select_device(devices)


def resource_budget():
    cgroup = Path('/sys/fs/cgroup')
    limit = (cgroup / 'memory.max').read_text().strip()
    avail = next(int(x.split()[1])*1024 for x in Path('/proc/meminfo').read_text().splitlines() if x.startswith('MemAvailable:'))
    if limit != 'max':
        current = int((cgroup / 'memory.current').read_text())
        stats = {k:int(v) for k,v in (s.split() for s in (cgroup/'memory.stat').read_text().splitlines())}
        avail = min(avail, int(limit) - current + stats.get('inactive_file',0))
    return avail


def verified(path, size, checksum):
    marker = ROOT / (path.name + '.verified')
    if not marker.is_file() or marker.read_text() != checksum or not path.is_file() or path.stat().st_size != size:
        return False
    if path.is_symlink():
        target = os.readlink(path)
        try:
            return bool(re.fullmatch(r'/proc/\d+/fd/\d+', target)) and os.readlink(target) == '/memfd:strata-iq3-s-model (deleted)'
        except FileNotFoundError:
            return False
    return True


def hold_file(fd):
    # Give a failed engine a short retry window without downloading its verified weights again.
    idle_since = time.monotonic()
    while time.monotonic() - idle_since < 600:
        with (ROOT/'worker.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                idle_since = time.monotonic()
        time.sleep(5)
    os.close(fd)


def serve():
    index, gpu_name = probe_device()
    visible = os.environ.get('CUDA_VISIBLE_DEVICES')
    selector = visible.split(',')[index].strip() if visible else str(index)
    available = resource_budget()
    files = [ROOT / f'{CHECKPOINT}-{i:05}-of-00002.gguf' for i in (1, 2)]
    disk_free = shutil.disk_usage(ROOT).free
    missing = [not verified(path, size, checksum) for path, (size, checksum) in zip(files, SHARDS)]
    required = sum(size for absent, (size, _) in zip(missing, SHARDS) if absent)
    memory_main = missing[0] and disk_free < required + 4_000_000_000
    if memory_main and (available < SHARDS[0][0] + 5_500_000_000 or disk_free < required - SHARDS[0][0] + 4_000_000_000):
        raise ValueError('現在のPodの空きRAM・ディスクではIQ3_Sを準備できません。')
    status('本家Strataのエンジンを準備しています')
    for asset in ASSETS:
        install_asset(*asset)
    ram_fd = None
    for i in (1, 0):  # Keep the lookup table on disk, even between engine restarts.
        path = files[i]
        size, checksum = SHARDS[i]
        marker = ROOT / (path.name + '.verified')
        if not missing[i]:
            continue
        in_ram = i == 0 and memory_main
        if in_ram:
            ram_fd = os.memfd_create('strata-iq3-s-model')
            destination = Path(f'/proc/{os.getpid()}/fd/{ram_fd}')
            fd = ram_fd
        else:
            destination = ROOT / (path.name + '.part')
            fd = os.open(destination, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            official = MODEL_URL + path.name
            port = int(os.environ.get('RUNPOD_STRATA_SOURCE_PORT','0'))
            home = f'http://127.0.0.1:{port}/{path.name}' if 0 < port <= 65535 else None
            ranged_fetch(home or official, fd, size, f'Qwenのファイル {i+1}/2', fallback=official if home else None)
            status(f'Qwenのファイル {i+1}/2 を照合しています')
            if digest(destination) != checksum:
                raise ValueError('QwenのSHA-256検証に失敗しました。')
        finally:
            if not in_ram:
                os.close(fd)
        if in_ram:
            link = ROOT / (path.name + '.link')
            link.unlink(missing_ok=True)
            keeper = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--hold', str(fd)], pass_fds=(fd,), stdin=subprocess.DEVNULL)
            link.symlink_to(f'/proc/{keeper.pid}/fd/{fd}')
            link.replace(path)
        else:
            destination.replace(path)
        marker.write_text(checksum)
    status('公式ツールでQwenを準備しています')
    control, engine, pack = ROOT/'control', ROOT/'engine', ROOT/'pack'
    python = str(control/'python/bin/python3')
    env = dict(os.environ, CUDA_VISIBLE_DEVICES=selector, PYTHONUNBUFFERED='1',
               LD_LIBRARY_PATH=str(engine) + ':' + os.environ.get('LD_LIBRARY_PATH',''))
    subprocess.run([python,str(control/'tools/iq_pack.py'),'--gguf',str(files[0]),'--out',str(pack)], cwd=control, env=env, check=True)
    cfg = dict(exe=str(engine/'strata'), cwd=str(control), tokenizer=str(pack/'tokenizer'), model_name=MODEL,
               log=str(ROOT/'engine.log'), args=['--pack',str(pack),'--native',str(files[0]),'--ple-gguf',str(files[1]),
               '--expert-profile',str(control/'data/expert-profile.bin'),'--mmap-experts','--expert-cache','auto',
               '--prefill','auto','--spec','4','--max-context','8192','--pool-workers','6','--vram-reserve-mib','1536'])
    config = ROOT/'engine.json'
    config.write_text(json.dumps(cfg))
    status(f'{gpu_name}でQwenを読み込んでいます','starting')
    subprocess.run([python,'-m','serve.server','--engine','strata','--config',str(config),'--host','127.0.0.1','--port',str(PORT)],
                   cwd=control, env=env, check=True)


if __name__ == '__main__':
    if sys.argv[1:] == ['--preflight']:
        try:
            lockfile = ROOT / 'worker.lock'
            if lockfile.exists():
                with lockfile.open('r') as lock:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        sys.exit(0)  # The resident engine can already be using the card's VRAM.
            probe_device()
        except (ValueError, OSError, RuntimeError) as exc:
            sys.exit(str(exc))
        sys.exit(0)
    if len(sys.argv) == 3 and sys.argv[1] == '--hold':
        hold_file(int(sys.argv[2]))
        sys.exit(0)
    os.umask(0o077)
    ROOT.mkdir(parents=True, exist_ok=True)
    with (ROOT/'worker.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit(0)
        try:
            serve()
        except Exception as exc:
            status(str(exc)[:220], 'failed')
            raise
