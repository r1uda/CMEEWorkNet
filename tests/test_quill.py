import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from app.server import App, FileDownload

class QuillDownloadTests(unittest.TestCase):
    def call(self,path):
        result={}
        def start(status,headers):result.update(status=status,headers=dict(headers))
        body=App(None)({'REQUEST_METHOD':'GET','PATH_INFO':path,'QUERY_STRING':''},start)
        return result,body

    def test_stream_and_early_close(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as folder:
            package=Path(folder)/'test.apk'
            payload=b'PK'+b'x'*600000
            package.write_bytes(payload)
            with patch('app.server.QUILL_APK',package):
                response,stream=self.call('/downloads/quill.apk')
                self.assertEqual(response['status'],'200 OK')
                self.assertEqual(int(response['headers']['Content-Length']),len(payload))
                self.assertIn('attachment',response['headers']['Content-Disposition'])
                self.assertIsInstance(stream,FileDownload)
                self.assertEqual(b''.join(stream),payload)
                self.assertTrue(stream.file.closed)
                _,stream=self.call('/downloads/quill.apk')
                iterator=iter(stream)
                self.assertLess(len(next(iterator)),len(payload))
                stream.close()
                self.assertTrue(stream.file.closed)

    def test_missing_and_path_escape(self):
        with patch('app.server.QUILL_APK',Path('does-not-exist.apk')):
            result,body=self.call('/downloads/quill.apk')
            self.assertTrue(result['status'].startswith('404'))
        for path in ('/downloads/../.env','/quill/../../.env','/quill/unknown.png'):
            result,body=self.call(path)
            self.assertTrue(result['status'].startswith('404'))

    def test_allowed_image(self):
        result,body=self.call('/quill/icon.png')
        self.assertEqual(result['status'],'200 OK')
        self.assertTrue(b''.join(body).startswith(b'\x89PNG'))

if __name__=='__main__':unittest.main()

