import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import urlopen, Request
import json
from urllib.parse import unquote

from companion import CompanionServer, State, PackageInfo


class ConnectedProtocolTest(unittest.TestCase):
    def test_status_empty_and_populated_library(self):
        with tempfile.TemporaryDirectory() as root:
            library = Path(root) / 'library'
            library.mkdir()
            state = State(library)
            server = CompanionServer(('127.0.0.1', 0), state)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f'http://127.0.0.1:{server.server_port}'

            def get(path):
                with urlopen(base + path, timeout=3) as response:
                    self.assertEqual(response.status, 200)
                    return response.read().decode('utf-8')

            try:
                self.assertEqual(len(get('/api/v1/status.txt').split('\t')), 6)
                self.assertEqual(get('/api/v1/library.txt'), '')
                state.scan = lambda: None
                state.packages['fixture'] = PackageInfo(
                    id='fixture', filename='demo.pkg', path=str(library / 'demo.pkg'),
                    title='Demo\t100% test', title_id='BREW05001', content_id='',
                    version='01.00', size=123, declared_size=123, content_type=0,
                    flags=0, is_patch=False, package_type='PS4GD', digest='',
                    icon_offset=0, icon_size=0, valid=True)
                (library / 'demo.pkg').write_bytes(b'0123456789')
                state.packages['fixture'].size = 10
                state.packages['fixture'].declared_size = 10
                descriptor = json.loads(get('/ref/fixture.json'))
                self.assertEqual(descriptor['originalFileSize'], 10)
                self.assertEqual(descriptor['pieces'][0]['url'], base + '/pkg/fixture')
                with urlopen(Request(base + '/pkg/fixture', headers={'Range': 'bytes=5-999'}), timeout=3) as response:
                    self.assertEqual(response.status, 206)
                    self.assertEqual(response.headers['Content-Range'], 'bytes 5-9/10')
                    self.assertEqual(response.read(), b'56789')
                row = get('/api/v1/library.txt').split('\t')
                self.assertEqual(len(row), 16)
                self.assertEqual(row[15], '')
                self.assertEqual(row[14], '1')
                self.assertEqual(unquote(row[1]), 'Demo\t100% test')
                state.packages['fixture'].valid = False
                state.packages['fixture'].error = 'Bad\nheader'
                row = get('/api/v1/library.txt').split('\t')
                self.assertEqual(row[14], '0')
                self.assertEqual(unquote(row[15]), 'Bad\nheader')
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)


if __name__ == '__main__':
    unittest.main()
