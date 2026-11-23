"""Serve only the two verified public model shards over the Pod's SSH connection."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import re
import selectors
import subprocess
import threading
from time import monotonic

from strata_pod import CHECKPOINT


class ModelSource:
    def __init__(self, paths, ssh):
        self.server = self.tunnel = None
        self.port = None
        if len(paths) != 2:
            return
        sources = {f'/{CHECKPOINT}-{i:05}-of-00002.gguf':p for i,p in enumerate(paths,1)}
        class Weights(BaseHTTPRequestHandler):
            def do_GET(self):
                path = sources.get(self.path)
                match = re.fullmatch(r'bytes=(\d+)-(\d+)', self.headers.get('Range',''))
                if path is None or not match:
                    self.send_error(404)
                    return
                start, end = map(int, match.groups())
                with path.open('rb') as stream:
                    size = os.fstat(stream.fileno()).st_size
                    if not 0 <= start <= end < size:
                        self.send_error(416)
                        return
                    self.send_response(206)
                    self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
                    self.send_header('Content-Length', str(end-start+1))
                    self.end_headers()
                    stream.seek(start)
                    left = end-start+1
                    try:
                        while left:
                            data = stream.read(min(left, 1 << 20))
                            if not data:
                                break
                            self.wfile.write(data)
                            left -= len(data)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(('127.0.0.1',0), Weights)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        local = self.server.server_port
        try:
            self.tunnel = subprocess.Popen([*ssh[:-1], '-oExitOnForwardFailure=yes','-oLogLevel=INFO',
                                            '-N','-R', f'127.0.0.1:0:127.0.0.1:{local}',ssh[-1]],
                                           stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
            with selectors.DefaultSelector() as sel:
                sel.register(self.tunnel.stderr,selectors.EVENT_READ)
                deadline = monotonic()+12
                while self.tunnel.poll() is None and monotonic()<deadline:
                    if not sel.select(max(0,deadline-monotonic())):
                        break
                    line = self.tunnel.stderr.readline()
                    match = re.search(rb'Allocated port ([0-9]+) for remote forward',line)
                    if match:
                        self.port = int(match.group(1))
                        return
            self.close()
        except OSError:
            self.close()

    def close(self):
        if self.tunnel is not None:
            self.tunnel.terminate()
            try:
                self.tunnel.wait(3)
            except subprocess.TimeoutExpired:
                self.tunnel.kill()
                self.tunnel.wait()
            self.tunnel.stderr.close()
            self.tunnel = None
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
