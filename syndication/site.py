import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@contextmanager
def serve():
    state = {'items': [
        {'id': 'notice-1', 'revision': 1, 'title': '授权公告', 'body': '华东发行许可', 'path': '/notice/1'},
        {'id': 'notice-2', 'revision': 1, 'title': '授权公告', 'body': '西部发行许可', 'path': '/notice/2'},
    ], 'redirects': {}, 'withdrawn': set(), 'status': {}}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            base = f'http://127.0.0.1:{self.server.server_port}'
            if self.path == '/feed' or self.path.startswith('/feed/'):
                source = 'publisher'
                if self.path.startswith('/feed/'):
                    source = self.path[len('/feed/'):]
                items = [item for item in state['items']
                         if item.get('source', 'publisher') == source]
                data = json.dumps({'source': source, 'items': [
                    {**item, 'url': base + item['path']} for item in items]},
                    ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
            elif self.path in state['redirects']:
                self.send_response(302)
                self.send_header('Location', state['redirects'][self.path])
                self.end_headers()
                return
            elif self.path in state['withdrawn'] or self.path in state['status']:
                code = state['status'].get(self.path, 410)
                self.send_error(code)
                return
            else:
                record = next((item for item in state['items'] if item['path'] == self.path), None)
                if record is None:
                    self.send_error(404)
                    return
                data = record['body'].encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
