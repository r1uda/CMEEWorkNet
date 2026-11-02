"""One-time, user-authorized completion of existing Excel-imported tasks."""
import json
from .core import audit, js, now, permit, record_view, row, rows, uid

KEY = 'maintenance:complete_legacy_tasks_20261101'


def complete(db, actor):
    permit(actor, 'can_manage_members')
    if row(db, 'SELECT key FROM settings WHERE key=?', (KEY,)):
        return dict(already_applied=True)
    stamp = now()
    changed = []
    records = rows(db, '''SELECT r.* FROM registrations r WHERE EXISTS
        (SELECT 1 FROM record_import_sources i WHERE i.registration_id=r.id)''')
    for record in records:
        # Original import snapshot distinguishes imported tasks from later additions.
        history = row(db, '''SELECT snapshot FROM record_history WHERE registration_id=?
            ORDER BY created_at,rowid LIMIT 1''', (record['id'],))
        if not history:
            raise ValueError('Imported record has no original history: ' + record['id'])
        imported_ids = {t['id'] for t in json.loads(history['snapshot'])['tasks']}
        tasks = [t for t in rows(db, 'SELECT * FROM tasks WHERE registration_id=?', (record['id'],))
                 if t['id'] in imported_ids and t['status'] != 'done']
        if not tasks:
            continue
        db.execute('INSERT INTO record_history VALUES (?,?,?,?,?)',
                   (uid(), record['id'], actor['id'], js(record_view(db, record)), stamp))
        for task in tasks:
            db.execute("UPDATE tasks SET status='done' WHERE id=?", (task['id'],))
            db.execute('INSERT INTO progress VALUES (?,?,?,?,?,?)',
                       (uid(), task['id'], actor['id'], '按确认统一将旧 Excel 导入事务标为已完成', 'done', stamp))
            changed.append(task['id'])
        db.execute('UPDATE registrations SET version=version+1,updated_at=? WHERE id=?', (stamp, record['id']))
        saved = record_view(db, row(db, 'SELECT * FROM registrations WHERE id=?', (record['id'],)))
        db.execute('INSERT INTO record_history VALUES (?,?,?,?,?)',
                   (uid(), record['id'], actor['id'], js(saved), stamp))
    result = dict(imported_records=len(records), completed_tasks=len(changed), task_ids=changed)
    audit(db, actor['id'], 'records.complete_legacy_imports', result)
    db.execute('INSERT INTO settings VALUES (?,?)', (KEY, js(result)))
    return result
