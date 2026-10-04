from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest

from test_deployment import load, SCRATCH

check = load('check_deploy', 'check-deploy.py')


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'assets').mkdir()
        self.js = b'export const value = 42;'
        self.css = b'body { color: red; }'
        (self.root / 'assets/app-12345678.js').write_bytes(self.js)
        (self.root / 'assets/app-12345678.css').write_bytes(self.css)
        self.routes = {'/': (200, 'text/html', b'<html><script src="/assets/app-12345678.js"></script><link rel="stylesheet" href="/assets/app-12345678.css"></html>'),
                       '/assets/app-12345678.js': (200, 'text/javascript', self.js),
                       '/assets/app-12345678.css': (200, 'text/css', self.css)}
        routes = self.routes

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                status, mime, body = routes.get(self.path, (404, 'text/html', b'<html>missing</html>'))
                self.send_response(status)
                self.send_header('Content-Type', mime)
                self.send_header('CF-Ray', 'test-ray')
                self.send_header('Retry-After', '30')
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.config = dict(output_dir=str(self.root), web_root='.', immutable_dirs=['assets'],
                           deploy_url=f'http://127.0.0.1:{self.server.server_port}/', max_assets=6)
        self.evidence = self.root / 'evidence'

    def test_valid_html_javascript_and_css(self):
        check.verify(self.config, self.evidence)
        self.assertEqual(len(list(self.evidence.glob('*.json'))), 3)

    def test_429_preserves_cloudflare_and_retry_diagnostics(self):
        self.routes['/'] = (429, 'text/html', b'<html>rate limited</html>')
        with self.assertRaisesRegex(ValueError, 'HTTP 429'):
            check.verify(self.config, self.evidence)
        record = json.loads((self.evidence / '000.json').read_text())
        self.assertEqual(record['cf_ray'], 'test-ray')
        self.assertEqual(record['retry_after'], '30')
        self.assertEqual(record['status'], 429)
        self.assertIn('publication is reported separately', record['verification_note'])
        self.assertIn('time_utc', record)
        self.assertIn(b'rate limited', (self.evidence / '000.body').read_bytes())

    def test_html_with_200_instead_of_javascript_fails(self):
        self.routes['/assets/app-12345678.js'] = (200, 'text/html', b'<html>error</html>')
        with self.assertRaisesRegex(ValueError, 'MIME'):
            check.verify(self.config, self.evidence)

    def test_wrong_asset_bytes_fail(self):
        self.routes['/assets/app-12345678.js'] = (200, 'text/javascript', b'old build')
        with self.assertRaisesRegex(ValueError, 'differ'):
            check.verify(self.config, self.evidence)

    def test_stale_html_references_fail(self):
        self.routes['/'] = (200, 'text/html', b'<html><script src="/assets/deleted-12345678.js"></script></html>')
        with self.assertRaisesRegex(ValueError, 'absent'):
            check.verify(self.config, self.evidence)
        self.assertTrue((self.evidence / 'dependency-error.json').exists())

    def test_missing_css_fails(self):
        self.routes['/assets/app-12345678.css'] = (404, 'text/html', b'<html>missing</html>')
        with self.assertRaisesRegex(ValueError, 'HTTP 404'):
            check.verify(self.config, self.evidence)

    def test_inline_sveltekit_import_detects_stale_chunk(self):
        self.routes['/'] = (200, 'text/html', b'<html><script>import("/assets/deleted-12345678.js")</script></html>')
        with self.assertRaisesRegex(ValueError, 'absent'):
            check.verify(self.config, self.evidence)


if __name__ == '__main__':
    unittest.main()
