import json
import sqlite3
from pathlib import Path
from app import handoff, service
from app.core import ROOT, Store, Problem, js, now, row, rows
from tests.test_business import BusinessTests


class HandoffChatTests(BusinessTests):
    def test_imported_pending_is_history_until_followed(self):
        r=self.save(tasks=[dict(text='旧表未完成事项',status='pending')])
        task=r['tasks'][0]
        with self.store.write() as db:
            db.execute('INSERT INTO record_import_previews VALUES (?,?,?,?,?,?)',('preview','m01','{}','baseline',now(),now()))
            db.execute('INSERT INTO record_import_sources VALUES (?,?,?,?)',('fingerprint',r['id'],'preview','Sheet1!2'))
            # Older progress notes must not silently opt imported work into current todos.
            db.execute('INSERT INTO progress VALUES (?,?,?,?,?,?)',('old-progress',task['id'],self.user['id'],'旧进展','pending',now()))
            before=handoff.personal_tasks(db,self.user)
            self.assertEqual(before['todos'],[])
            self.assertEqual([x['id'] for x in before['historical_tasks']],[task['id']])
            self.assertEqual(handoff.personal_tasks(db,self.admin)['historical_tasks'],[])
            handoff.follow(db,self.user,dict(task_id=task['id']))
            after=handoff.personal_tasks(db,self.user)
            self.assertEqual(after['historical_tasks'],[])
            self.assertEqual([x['id'] for x in after['todos']],[task['id']])
            self.assertEqual(row(db,'SELECT status FROM tasks WHERE id=?',(task['id'],))['status'],'pending')

    def test_new_direct_handoff_accept_chat_and_finish_without_registration(self):
        with self.store.write() as db:
            before=db.execute('SELECT COUNT(*) FROM registrations').fetchone()[0]
            h=service.handoff_create(db,self.user,dict(recipient_id='m03',text='明天领取材料',reason='请去办公室领取'))
            self.assertEqual(handoff.personal_tasks(db,self.other)['todos'],[])
            self.assertEqual(len(handoff.personal_tasks(db,self.user)['todos']),1)
            handoff.comment(db,self.other,dict(id=h['id'],text='具体哪个办公室？'))
            handoff.comment(db,self.user,dict(id=h['id'],text='203'))
            self.assertEqual(len(handoff.threads(db,self.user)[0]['comments']),2)
            self.assertEqual(handoff.threads(db,self.admin),[])
            with self.assertRaises(Problem):handoff.comment(db,self.admin,dict(id=h['id'],text='越权留言'))
            with self.assertRaises(Problem):service.handoff_decide(db,self.user,dict(id=h['id'],decision='accept'))
            service.handoff_decide(db,self.other,dict(id=h['id'],decision='accept'))
            self.assertEqual(handoff.personal_tasks(db,self.user)['todos'],[])
            todo=handoff.personal_tasks(db,self.other)['todos'][0]
            with self.assertRaises(Problem):handoff.direct_progress(db,self.user,dict(task_id=todo['id'],text='越权',status='done'))
            handoff.direct_progress(db,self.other,dict(task_id=todo['id'],text='材料已领取',status='done'))
            self.assertEqual(handoff.personal_tasks(db,self.other)['todos'],[])
            self.assertIn('材料已领取',handoff.threads(db,self.user)[0]['comments'][-1]['text'])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM registrations').fetchone()[0],before)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM outbox WHERE kind='workflow'").fetchone()[0],5)

    def test_rejected_direct_handoff_stays_with_sender_and_can_transfer_again(self):
        with self.store.write() as db:
            h=service.handoff_create(db,self.user,dict(recipient_id='m03',text='核对资料'))
            task=handoff.personal_tasks(db,self.user)['todos'][0]
            with self.assertRaises(Problem):service.handoff_create(db,self.user,dict(recipient_id='m01',direct_task_id=task['id']))
            service.handoff_decide(db,self.other,dict(id=h['id'],decision='reject'))
            self.assertEqual(handoff.personal_tasks(db,self.other)['todos'],[])
            next_h=service.handoff_create(db,self.user,dict(recipient_id='m01',direct_task_id=task['id']))
            service.handoff_decide(db,self.admin,dict(id=next_h['id'],decision='accept'))
            self.assertEqual(handoff.personal_tasks(db,self.user)['todos'],[])
            self.assertEqual(handoff.personal_tasks(db,self.admin)['todos'][0]['id'],task['id'])

    def test_drafts_and_other_owners_never_become_my_todos(self):
        r=self.save(tasks=[dict(text='待处理',status='pending')])
        with self.store.write() as db:
            self.assertEqual(handoff.personal_tasks(db,self.other)['todos'],[])
            db.execute("UPDATE registrations SET state='draft' WHERE id=?",(r['id'],))
            self.assertEqual(handoff.personal_tasks(db,self.user)['todos'],[])
            with self.assertRaises(Problem):handoff.follow(db,self.user,dict(task_id=r['tasks'][0]['id']))

    def test_migration_preserves_existing_handoffs_and_restarts(self):
        path=Path(self.tmp.name)/'old.sqlite3'
        db=sqlite3.connect(path)
        db.executescript((ROOT/'app/schema.sql').read_text('utf-8'))
        for migration in sorted((ROOT/'app/migrations').glob('*.sql')):
            version=int(migration.name.split('_')[0])
            if version>=6:break
            db.executescript(migration.read_text('utf-8'))
            db.execute('INSERT INTO schema_version VALUES (?)',(version,))
        for mid in ('m02','m03'):db.execute('INSERT INTO members(id,name) VALUES (?,?)',(mid,mid))
        db.execute("INSERT INTO registrations VALUES ('r',NULL,'m02','2026-10-08',NULL,'submitted','',1,?,?,?)",(now(),now(),now()))
        db.execute("INSERT INTO tasks VALUES ('t','r','保留旧交接','pending',1,'m02')")
        db.execute("INSERT INTO handoffs VALUES ('h','t','m02','m03','原说明','pending',?,NULL)",(now(),))
        db.commit();db.close()
        migrated=Store(path);migrated.init();migrated.init()
        with migrated.read() as db:
            self.assertEqual(row(db,'SELECT * FROM handoffs')['reason'],'原说明')
            self.assertEqual(row(db,'SELECT * FROM handoffs')['task_id'],'t')
            self.assertEqual(rows(db,'PRAGMA foreign_key_check'),[])
