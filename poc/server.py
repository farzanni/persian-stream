import http.client
import http.server
import os
import re

os.chdir('/home/farzan/dev/persian-stream/poc')


class H(http.server.SimpleHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def _cors(self):
        # CORS + Private Network Access so https embeds may fetch from us
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Headers', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, HEAD, OPTIONS')
        self.send_header('Access-Control-Allow-Private-Network', 'true')

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header('Content-Length', '0')
        self.end_headers()

    def _serve_file_range(self, send_body):
        """GET/HEAD with proper HTTP Range support."""
        path = self.translate_path(self.path)
        if not os.path.isfile(path):
            self.send_error(404, 'File not found')
            return
        size = os.path.getsize(path)
        start, end = 0, size - 1
        status = 200
        rng = self.headers.get('Range')
        m = re.match(r'bytes=(\d*)-(\d*)$', rng or '')
        if rng and m:
            first, last = m.group(1), m.group(2)
            if first == '' and last:
                start = max(0, size - int(last))          # suffix range
            else:
                start = int(first) if first else 0
                end = int(last) if last else size - 1
                end = min(end, size - 1)
            status = 206
            if start >= size or start > end:
                self.send_response(416)
                self.send_header('Content-Range', f'bytes */{size}')
                self._cors()
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
        length = end - start + 1
        self.send_response(status)
        self.send_header('Content-Type', self.guess_type(path))
        self.send_header('Content-Length', str(length))
        self.send_header('Accept-Ranges', 'bytes')
        if status == 206:
            self.send_header('Content-Range',
                             f'bytes {start}-{end}/{size}')
        self._cors()
        self.end_headers()
        if not send_body:
            return
        with open(path, 'rb') as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(65536, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                remaining -= len(chunk)

    def do_GET(self):
        self._serve_file_range(send_body=True)

    def do_HEAD(self):
        self._serve_file_range(send_body=False)

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (http.client.RemoteDisconnected, ConnectionResetError,
                BrokenPipeError):
            self.close_connection = True

    def log_message(self, fmt, *args):
        print('REQ |', self.command, '|', self.path, '| Origin:',
              self.headers.get('Origin'), flush=True)


class Srv(http.server.ThreadingHTTPServer):
    daemon_threads = True


print('threaded server on :8899 (real Range support)', flush=True)
Srv(('0.0.0.0', 8899), H).serve_forever()
