import tempfile
import unittest
from pathlib import Path
from app import service
from app.complete_legacy_tasks import complete
from app.core import CAPS, Store, js, member_by_id, now, row


class CompleteLegacyTests(unittest.TestCase):
    def test_scope_history_and_idempotency(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            store = Store(Path(folder) / 'db.sqlite3')
            store.init()
            with store.write() as db:
                db.execute('UPDATE members SET caps=? WHERE id=?', (js(CAPS), 'm03'))
                actor = member_by_id(db, 'm03')
                old = service.save_record(db, actor, dict(date='2026-10-01', state='submitted', tasks=[dict(text='旧导入', status='pending')]))
                fresh = service.save_record(db, actor, dict(date='2026-10-01', state='submitted', tasks=[dict(text='网页新增', status='pending')]))
                db.execute('INSERT INTO record_import_previews VALUES (?,?,?,?,?,?)', ('import', actor['id'], '{}', '', now(), now()))
                db.execute('INSERT INTO record_import_sources VALUES (?,?,?,?)', ('hash', old['id'], 'import', 'Sheet1!2'))
                db.execute('INSERT INTO tasks VALUES (?,?,?,?,?,?)', ('later', old['id'], '导入后追加', 'pending', 1, actor['id']))
                before = row(db, 'SELECT * FROM registrations WHERE id=?', (fresh['id'],))
                result = complete(db, actor)
                self.assertEqual(result['completed_tasks'], 1)
                self.assertEqual(row(db, 'SELECT status FROM tasks WHERE id=?', (old['tasks'][0]['id'],))['status'], 'done')
                self.assertEqual(row(db, 'SELECT status FROM tasks WHERE id=?', (fresh['tasks'][0]['id'],))['status'], 'pending')
                self.assertEqual(row(db, "SELECT status FROM tasks WHERE id='later'")['status'], 'pending')
                self.assertEqual(before, row(db, 'SELECT * FROM registrations WHERE id=?', (fresh['id'],)))
                self.assertEqual(db.execute('SELECT COUNT(*) FROM record_history WHERE registration_id=?', (old['id'],)).fetchone()[0], 3)
                self.assertTrue(complete(db, actor)['already_applied'])
                self.assertEqual(db.execute('SELECT COUNT(*) FROM progress').fetchone()[0], 1)
