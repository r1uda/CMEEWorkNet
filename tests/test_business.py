import base64
import datetime as dt
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile

from app import ai, auth, core, mailer, public_swap, service
from app.core import CAPS, SLOTS, TZ, Problem, Store, audit, digest, js, member_by_id, now, row, rows, uid
from app.server import App
from app.xlsx import NS, read_schedule, workbook


class BusinessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='business-',dir=Path(__file__).resolve().parent)
        self.store = Store(Path(self.tmp.name)/'test.sqlite3')
        self.store.init()
        self.env = patch.dict(os.environ,{'APP_URL':'http://127.0.0.1:8765','COOKIE_SECURE':'false'},clear=False)
        self.env.start()
        with self.store.write() as db:
            db.execute('UPDATE members SET caps=? WHERE id=?',(js(CAPS),'m01'))
            for member in ('m01','m02','m03'):
                db.execute('UPDATE members SET email=? WHERE id=?',(member+'@example.invalid',member))
        with self.store.read() as db:
            self.admin=member_by_id(db,'m01');self.user=member_by_id(db,'m02');self.other=member_by_id(db,'m03')
        self.today=dt.datetime.now(TZ).date()

    def tearDown(self):
        self.env.stop();self.tmp.cleanup()

    def test_home_order_is_saved_only_for_current_member(self):
        order = list(reversed(service.HOME_BLOCKS))
        with self.store.write() as db:
            service.save_home_order(db, self.user, {'order': order, 'member_id': self.other['id']})
        with self.store.read() as db:
            self.assertEqual(service.state(db, self.user)['me']['home_order'], order)
            self.assertEqual(service.state(db, self.other)['me']['home_order'], [])
            self.assertNotIn('home_order:' + self.user['id'], service.state(db, self.other)['settings'])

    def test_bound_member_can_request_login_while_background_mail_paused(self):
        with self.store.write() as db:
            db.execute('INSERT INTO settings VALUES (?,?)', ('mail_notifications:m02', 'false'))
        with patch.object(mailer, 'configured', return_value=True), patch.object(mailer, 'send_email') as send:
            auth.request_code(self.store, {'email': self.user['email']})
            send.assert_called_once()
            self.assertEqual(send.call_args.args[0], self.user['email'])
        with self.store.read() as db:
            for kind in ('reminder', 'reminder_escalation'):
                self.assertFalse(mailer.eligible(db, {'recipient_id':'m02', 'kind':kind}, dt.datetime.now(TZ)))
            self.assertTrue(mailer.notifications_enabled(db, 'm03'))

    def test_paused_member_existing_reminder_is_not_sent(self):
        self.activate()
        self.shift()
        at = dt.datetime.combine(self.today, dt.time(23), TZ)
        self.assertEqual(mailer.queue_due(self.store, at), 1)
        with self.store.write() as db:
            db.execute('INSERT INTO settings VALUES (?,?)', ('mail_notifications:m02', 'false'))
        with patch.object(mailer, 'send_email') as send:
            mailer.process_outbox(self.store, sender=send, at=at)
            send.assert_not_called()
        self.assertEqual(mailer.queue_due(self.store, at), 0)
        with self.store.read() as db:
            self.assertEqual(row(db, 'SELECT status FROM outbox')['status'], 'canceled')

    def test_sql_statements_split_on_boundaries(self):
        script = ("CREATE TABLE probe_a(id INTEGER); CREATE TABLE probe_b(id INTEGER);\n"
                  "INSERT INTO probe_a VALUES (1);\n"
                  "CREATE TABLE probe_c(note TEXT); INSERT INTO probe_c VALUES ('a;b');\n"
                  "-- 注释里的分号不该切: 1;\n"
                  "INSERT INTO probe_c VALUES (\"c;d\");\n")
        parts = core.sql_statements(script)
        self.assertEqual(len(parts), 6, parts)
        self.assertTrue(parts[0].startswith('CREATE TABLE probe_a'))
        self.assertTrue(parts[1].startswith('CREATE TABLE probe_b'))
        self.assertEqual(parts[4].count(';'), 1, parts[4])

    def test_migration_with_two_statements_on_one_line(self):
        with tempfile.TemporaryDirectory(prefix='mig-', dir=Path(__file__).resolve().parent) as tmp:
            folder = Path(tmp)
            (folder / '013_multi.sql').write_text(
                "CREATE TABLE probe_a(id INTEGER); CREATE TABLE probe_b(id INTEGER);\n"
                "INSERT INTO probe_a VALUES (1); INSERT INTO probe_b VALUES (2);\n"
                "CREATE TABLE probe_c(note TEXT); INSERT INTO probe_c VALUES ('a;b');\n",
                encoding='utf-8')
            with self.store.write() as db:
                core.apply_migrations(db, folder)
            with self.store.read() as db:
                names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for name in ('probe_a', 'probe_b', 'probe_c'):
                    self.assertIn(name, names)
                self.assertEqual(db.execute('SELECT note FROM probe_c').fetchone()[0], 'a;b')
                self.assertIsNotNone(db.execute('SELECT 1 FROM schema_version WHERE version=13').fetchone())

    def test_failed_migration_rolls_back_and_can_be_retried(self):
        with tempfile.TemporaryDirectory(prefix='mig-', dir=Path(__file__).resolve().parent) as tmp:
            folder = Path(tmp)
            broken = folder / '012_probe.sql'
            broken.write_text('CREATE TABLE review_probe(id INTEGER);\nINSERT INTO missing_table VALUES(1);\n', encoding='utf-8')
            with self.assertRaises(sqlite3.OperationalError):
                with self.store.write() as db:
                    core.apply_migrations(db, folder)
            with self.store.read() as db:
                self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='review_probe'").fetchone())
                self.assertIsNone(db.execute('SELECT 1 FROM schema_version WHERE version=12').fetchone())
            broken.write_text('CREATE TABLE review_probe(id INTEGER);\n', encoding='utf-8')
            with self.store.write() as db:
                core.apply_migrations(db, folder)
            with self.store.read() as db:
                self.assertIsNotNone(db.execute('SELECT 1 FROM schema_version WHERE version=12').fetchone())
                self.assertIsNotNone(db.execute("SELECT name FROM sqlite_master WHERE name='review_probe'").fetchone())

    def test_home_order_rejects_invalid_blocks(self):
        for order in (None, [], ['pending'] * 6, list(service.HOME_BLOCKS[:-1]) + ['stats'], [{}] * 6):
            with self.subTest(order=order), self.store.write() as db:
                with self.assertRaises(Problem):
                    service.save_home_order(db, self.user, {'order': order})

    def test_client_ip_trusts_forwarded_only_from_loopback(self):
        self.assertEqual(App.client_ip({'REMOTE_ADDR': '127.0.0.1', 'HTTP_X_FORWARDED_FOR': '203.0.113.9, 10.0.0.1'}), '203.0.113.9')
        self.assertEqual(App.client_ip({'REMOTE_ADDR': '198.51.100.7', 'HTTP_X_FORWARDED_FOR': '203.0.113.9'}), '198.51.100.7')
        self.assertEqual(App.client_ip({'REMOTE_ADDR': '127.0.0.1'}), '127.0.0.1')

    def test_static_asset_supports_conditional_request(self):
        def fetch(path, extra=None):
            environ = {'REQUEST_METHOD': 'GET', 'PATH_INFO': path, 'QUERY_STRING': '',
                       'wsgi.input': io.BytesIO(b''), 'CONTENT_LENGTH': '0'}
            if extra:
                environ.update(extra)
            seen = {}
            def start(status, headers):
                seen.update(status=int(status.split()[0]), headers=dict(headers))
            return seen, b''.join(App(self.store)(environ, start))
        first, body = fetch('/app.js')
        self.assertEqual(first['status'], 200)
        self.assertTrue(body)
        etag = first['headers'].get('ETag')
        self.assertTrue(etag)
        self.assertEqual(first['headers'].get('Cache-Control'), 'no-cache')
        second, empty = fetch('/app.js', {'HTTP_IF_NONE_MATCH': etag})
        self.assertEqual(second['status'], 304)
        self.assertEqual(empty, b'')

    def test_audit_detail_is_truncated(self):
        with self.store.write() as db:
            audit(db, 'm01', 'settings.save', {'payload': 'x' * 8000})
        with self.store.read() as db:
            detail = json.loads(row(db, "SELECT detail FROM audit WHERE action='settings.save'")['detail'])
        self.assertTrue(detail.get('truncated'))
        self.assertLess(len(detail['preview']), 4000)

    def test_public_swap_audience_is_cleaned_when_canceled(self):
        shift = self.shift(member='m01', date=self.today + dt.timedelta(days=5))
        with self.store.write() as db:
            created = public_swap.create(db, self.admin, dict(shift_id=shift, version=1, mail_audience=['m02']))
            key = 'public_swap_mail_audience:' + created['id']
            self.assertIsNotNone(row(db, 'SELECT value FROM settings WHERE key=?', (key,)))
            public_swap.cancel(db, self.admin, dict(id=created['id']))
            self.assertIsNone(row(db, 'SELECT value FROM settings WHERE key=?', (key,)))

    def shift(self,member='m02',date=None,slot=0):
        ident=uid()
        with self.store.write() as db:
            db.execute('INSERT INTO shifts(id,date,slot,original_member_id,member_id) VALUES (?,?,?,?,?)',(ident,str(date or self.today),slot,member,member))
        return ident

    def save(self,shift_id=None,user=None,state='submitted',tasks=None,**extra):
        with self.store.write() as db:
            return service.save_record(db,user or self.user,dict(shift_id=shift_id,shift_version=1,date=str(self.today),state=state,tasks=tasks or [],notes='',**extra))

    def activate(self,date=None):
        with self.store.write() as db:
            db.execute('UPDATE settings SET value=? WHERE key=?',(js(str(date or self.today)),'activation_date'))

    def call(self,route,body=None,session=None,csrf=None,method=None,origin='http://127.0.0.1:8765',app=None):
        payload=js(body or {}).encode()
        result={}
        environ={'REQUEST_METHOD':method or ('POST' if body is not None else 'GET'),'PATH_INFO':'/api/'+route,'QUERY_STRING':'','wsgi.input':io.BytesIO(payload),'CONTENT_LENGTH':str(len(payload)),'CONTENT_TYPE':'application/json','HTTP_ORIGIN':origin,'REMOTE_ADDR':'127.0.0.1'}
        if session:environ['HTTP_COOKIE']='wb_session='+session
        if csrf:environ['HTTP_X_CSRF_TOKEN']=csrf
        def start(status,headers):result.update(status=int(status.split()[0]),headers=dict(headers))
        raw=b''.join((app or App(self.store))(environ,start))
        result['body']=json.loads(raw)
        return result

    def login(self,member=None):
        with self.store.write() as db:return auth.create_session(db,member or self.user)

    def test_login_mail_and_validator_use_exactly_six_digits(self):
        with patch('app.auth.mailer.configured',return_value=True),patch('app.auth.secrets.randbelow',return_value=12345),patch('app.auth.mailer.send_email') as sender:
            auth.request_code(self.store,dict(email='m02@example.invalid'))
        body=sender.call_args.args[2]
        self.assertIn('\n\n012345\n\n',body)
        self.assertNotIn('10',body)
        with self.assertRaises(Problem):auth.verify_code(self.store,dict(email='m02@example.invalid',code='01234510'))
        self.assertTrue(auth.verify_code(self.store,dict(email='m02@example.invalid',code='012345'))['token'])

    def test_management_routes_reject_ordinary_account_even_with_shared_records(self):
        session=self.login()
        with self.store.write() as db:
            db.execute("UPDATE settings SET value='true' WHERE key='share_records'")
        for route,body in [('workload',None),('attendance',None),('audit',None),('records/template',None),
                           ('records/import-preview',{}),('records/import-confirm',{}),('records/admin-save',{})]:
            reply=self.call(route,body,session=session['token'],csrf=session['csrf'])
            self.assertEqual(reply['status'],403,route)

    def test_local_ai_requires_explicit_flag_and_preserves_mail_block(self):
        session=self.login()
        with patch('app.ai.configured',return_value=True),patch('app.ai.ask',return_value=dict(message='模型回答',sources=[],proposal=None)) as ask:
            regular=App(self.store,demo=True)
            self.assertFalse(self.call('bootstrap',app=regular)['body']['ai_configured'])
            self.assertEqual(self.call('ai/ask',dict(message='你好',consent=True),session=session['token'],csrf=session['csrf'],app=regular)['status'],503)
            connected=App(self.store,demo=True,demo_ai=True)
            info=self.call('bootstrap',app=connected)['body']
            self.assertTrue(info['ai_configured']);self.assertTrue(info['demo_ai']);self.assertFalse(info['mail_configured'])
            self.assertEqual(self.call('ai/ask',dict(message='你好',consent=True),session=session['token'],csrf=session['csrf'],app=connected)['status'],200)
            self.assertEqual(ask.call_count,1)
            self.assertEqual(self.call('auth/code',dict(email='m02@example.invalid'),app=connected)['status'],400)
            with self.assertRaises(Problem):connected.dispatch({'REMOTE_ADDR':'192.0.2.1'})
            with self.assertRaises(Problem):App(self.store,demo_ai=True)

    def test_empty_submission_cancels_reminder(self):
        shift=self.shift();self.activate();at=dt.datetime.combine(self.today,dt.time(10,10),TZ)
        self.assertEqual(mailer.queue_due(self.store,at),1)
        record=self.save(shift)
        self.assertEqual(record['state'],'submitted');self.assertEqual(record['tasks'],[])
        sent=[];mailer.process_outbox(self.store,lambda *args:sent.append(args),at)
        self.assertEqual(sent,[])
        with self.store.read() as db:self.assertEqual(row(db,'SELECT status FROM outbox')['status'],'canceled')

    def test_draft_does_not_count_as_submission_and_schedule_boundaries(self):
        self.activate()
        for slot in range(4):self.shift(slot=slot)
        with self.store.read() as db:shift=row(db,'SELECT id FROM shifts WHERE slot=0')['id']
        self.save(shift,state='draft')
        for hour,minute,expected in [(10,9,0),(10,10,1),(12,9,1),(12,10,2),(16,10,3),(18,10,4)]:
            at=dt.datetime.combine(self.today,dt.time(hour,minute),TZ)
            mailer.queue_due(self.store,at)
            with self.store.read() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0],expected)

    def test_reminder_deduplicates_repeated_workers_and_retries(self):
        self.shift();self.activate();at=dt.datetime.combine(self.today,dt.time(10,10),TZ)
        for _ in range(3):mailer.queue_due(self.store,at)
        calls=[]
        for _ in range(3):mailer.process_outbox(self.store,lambda *args:calls.append(args),at)
        self.assertEqual(len(calls),1)
        with self.store.read() as db:self.assertEqual(row(db,'SELECT status FROM outbox')['status'],'sent')

    def test_reminder_does_not_include_disabled_history_or_future(self):
        self.shift(date=self.today-dt.timedelta(days=1));self.shift(date=self.today+dt.timedelta(days=1));self.shift(slot=1)
        at=dt.datetime.combine(self.today,dt.time(10,10),TZ)
        self.assertEqual(mailer.queue_due(self.store,at),0)
        self.activate();self.assertEqual(mailer.queue_due(self.store,at),0)

    def test_uncertain_send_is_not_automatically_retried(self):
        self.shift();self.activate();at=dt.datetime.combine(self.today,dt.time(10,10),TZ)
        mailer.queue_due(self.store,at)
        calls=[]
        def sender(*args):calls.append(args);raise mailer.UncertainDelivery('unknown')
        mailer.process_outbox(self.store,sender,at)
        mailer.queue_due(self.store,at+dt.timedelta(hours=1));mailer.process_outbox(self.store,sender,at+dt.timedelta(hours=1))
        self.assertEqual(len(calls),1)
        with self.store.read() as db:self.assertEqual(row(db,'SELECT status FROM outbox')['status'],'uncertain')

    def test_failed_send_retries_same_business_task(self):
        self.shift();self.activate();at=dt.datetime.combine(self.today,dt.time(10,10),TZ)
        mailer.queue_due(self.store,at)
        def fail(*args):raise RuntimeError('unavailable')
        mailer.process_outbox(self.store,fail,at)
        calls=[];mailer.process_outbox(self.store,lambda *args:calls.append(args),at+dt.timedelta(minutes=3))
        self.assertEqual(len(calls),1)
        with self.store.read() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0],1)

    def test_admin_cannot_modify_another_authors_record_via_api(self):
        r=self.save(tasks=[dict(text='原作者内容',status='pending')]);session=self.login(self.admin)
        body=dict(r);body.pop('author_id');body['notes']='篡改'
        reply=self.call('records/save',body,session['token'],session['csrf'])
        self.assertEqual(reply['status'],403)
        with self.store.read() as db:self.assertEqual(row(db,'SELECT notes FROM registrations WHERE id=?',(r['id'],))['notes'],'')

    def test_author_spoofing_and_csrf_rejected(self):
        session=self.login()
        base=dict(date=str(self.today),state='submitted',tasks=[])
        self.assertEqual(self.call('records/save',dict(base,author_id='m01'),session['token'],session['csrf'])['status'],403)
        self.assertEqual(self.call('records/save',base,session['token'],'wrong')['status'],403)
        self.assertEqual(self.call('records/save',base,session['token'],session['csrf'],origin='https://evil.invalid')['status'],403)
        self.assertEqual(self.call('auth/demo',dict(id='m01'))['status'],403)

    def test_register_and_manage_capabilities_are_independent(self):
        with self.store.write() as db:
            db.execute('UPDATE members SET caps=? WHERE id=?',(js(['can_manage_schedule']),'m03'))
            member=member_by_id(db,'m03')
            with self.assertRaises(Problem):service.save_record(db,member,dict(date=str(self.today),state='submitted',tasks=[]))

    def test_submission_is_unique_and_optimistic_version_protects(self):
        shift=self.shift();r=self.save(shift)
        with self.assertRaises(Problem):self.save(shift)
        with self.store.write() as db:
            body=dict(id=r['id'],version=1,shift_version=1,state='submitted',tasks=[],notes='第一次修改')
            updated=service.save_record(db,self.user,body)
            self.assertEqual(updated['version'],2)
        with self.store.write() as db:
            with self.assertRaises(Problem):service.save_record(db,self.user,body)
        with self.store.read() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM record_history').fetchone()[0],2)

    def test_draft_and_submitted_visibility(self):
        self.save(state='draft');self.save(state='submitted')
        with self.store.write() as db:
            self.assertEqual(len(service.state(db,self.other)['records']),0)
            db.execute("UPDATE settings SET value='true' WHERE key='share_records'")
            records=service.state(db,self.other)['records']
            self.assertEqual(len(records),1);self.assertEqual(records[0]['state'],'submitted')
            self.assertNotIn('email',service.state(db,self.other)['members'][0])

    def test_submitted_record_with_unfinished_task_stops_reminder(self):
        shift=self.shift();self.activate();r=self.save(shift,tasks=[dict(text='待办',status='pending')])
        self.assertEqual(r['tasks'][0]['status'],'pending')
        self.assertEqual(mailer.queue_due(self.store,dt.datetime.combine(self.today,dt.time(20),TZ)),0)

    def test_swap_pending_keeps_original_accept_changes_responsibility(self):
        shift=self.shift(date=self.today+dt.timedelta(days=2))
        with self.store.write() as db:
            request=service.swap_create(db,self.user,dict(shift_id=shift,version=1,recipient_id='m03'))
            self.assertEqual(row(db,'SELECT member_id FROM shifts WHERE id=?',(shift,))['member_id'],'m02')
            service.swap_decide(db,self.other,dict(id=request['id'],decision='accept'))
            self.assertEqual(row(db,'SELECT member_id FROM shifts WHERE id=?',(shift,))['member_id'],'m03')
            with self.assertRaises(Problem):service.swap_decide(db,self.other,dict(id=request['id'],decision='accept'))

    def test_swap_atomic_exchange_and_stale_version(self):
        date=self.today+dt.timedelta(days=2);a=self.shift(date=date);b=self.shift(member='m03',date=date,slot=1)
        with self.store.write() as db:
            request=service.swap_create(db,self.user,dict(shift_id=a,version=1,recipient_id='m03',target_shift_id=b,target_version=1))
            service.swap_decide(db,self.other,dict(id=request['id'],decision='accept'))
            self.assertEqual(row(db,'SELECT member_id FROM shifts WHERE id=?',(a,))['member_id'],'m03')
            self.assertEqual(row(db,'SELECT member_id FROM shifts WHERE id=?',(b,))['member_id'],'m02')

    def test_swap_old_mail_is_invalidated_and_recipient_rechecked(self):
        date=self.today+dt.timedelta(days=2);shift=self.shift(date=date);self.activate()
        at=dt.datetime.combine(date,dt.time(10,10),TZ)
        mailer.queue_due(self.store,at)
        with self.store.write() as db:
            request=service.swap_create(db,self.user,dict(shift_id=shift,version=1,recipient_id='m03'))
            service.swap_decide(db,self.other,dict(id=request['id'],decision='accept'))
        mailer.queue_due(self.store,at);calls=[]
        mailer.process_outbox(self.store,lambda *args:calls.append(args),at)
        self.assertEqual(len(calls),2);self.assertCountEqual([c[0] for c in calls],['m02@example.invalid','m03@example.invalid'])

    def test_two_simultaneous_accepts_do_not_transfer_twice(self):
        shift=self.shift(date=self.today+dt.timedelta(days=2))
        with self.store.write() as db:request=service.swap_create(db,self.user,dict(shift_id=shift,version=1,recipient_id='m03'))
        results=[]
        def accept():
            try:
                with self.store.write() as db:results.append(service.swap_decide(db,self.other,dict(id=request['id'],decision='accept'))['state'])
            except Problem:results.append('conflict')
        threads=[threading.Thread(target=accept) for _ in range(2)]
        for t in threads:t.start()
        for t in threads:t.join()
        self.assertCountEqual(results,['accepted','conflict'])

    def test_handoff_preserves_author_and_original_text(self):
        r=self.save(tasks=[dict(text='原始任务正文',status='pending')]);task=r['tasks'][0]
        with self.store.write() as db:
            request=service.handoff_create(db,self.user,dict(task_id=task['id'],recipient_id='m03',reason='请接着处理'))
            service.handoff_decide(db,self.other,dict(id=request['id'],decision='accept'))
            service.progress_add(db,self.other,dict(task_id=task['id'],text='已处理完成',status='done'))
            saved=service.record_view(db,row(db,'SELECT * FROM registrations WHERE id=?',(r['id'],)))
            self.assertEqual(saved['author_id'],'m02');self.assertEqual(saved['tasks'][0]['text'],'原始任务正文')
            self.assertEqual(saved['tasks'][0]['progress'][0]['author_id'],'m03')
            with self.assertRaises(Problem):service.progress_add(db,self.user,dict(task_id=task['id'],text='越权进展',status='done'))

    def test_message_read_confirm_and_swap_are_distinct(self):
        with self.store.write() as db:
            m=service.message_send(db,self.user,dict(recipients=['m03'],body='测试通知'))
            service.message_mark(db,self.other,dict(id=m['id'],kind='read'))
            self.assertIsNone(row(db,'SELECT confirmed_at FROM message_recipients')['confirmed_at'])
            service.message_mark(db,self.other,dict(id=m['id'],kind='confirm'))
            self.assertIsNotNone(row(db,'SELECT confirmed_at FROM message_recipients')['confirmed_at'])

    def test_group_announcement_requires_independent_permission(self):
        with self.store.write() as db:
            with self.assertRaises(Problem):service.message_send(db,self.user,dict(announcement=True,body='越权公告'))
            message=service.message_send(db,self.admin,dict(announcement=True,body='全组通知'))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM message_recipients WHERE message_id=?',(message['id'],)).fetchone()[0],9)

    def test_reenabled_reminder_range_can_resume_previously_canceled_job(self):
        self.shift();self.activate();at=dt.datetime.combine(self.today,dt.time(10,10),TZ)
        mailer.queue_due(self.store,at)
        with self.store.write() as db:service.settings_save(db,self.admin,dict(activation_date=''))
        self.activate();mailer.queue_due(self.store,at);calls=[]
        mailer.process_outbox(self.store,lambda *args:calls.append(args),at)
        self.assertEqual(len(calls),1)
        with self.store.read() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0],1)

    def test_worker_restart_marks_interrupted_send_uncertain(self):
        self.shift();self.activate();at=dt.datetime.combine(self.today,dt.time(10,10),TZ)
        mailer.queue_due(self.store,at)
        with self.store.write() as db:db.execute("UPDATE outbox SET status='sending'")
        mailer.recover_interrupted(self.store)
        calls=[];mailer.process_outbox(self.store,lambda *args:calls.append(args),at)
        self.assertEqual(calls,[])
        with self.store.read() as db:self.assertEqual(row(db,'SELECT status FROM outbox')['status'],'uncertain')

    def test_worker_singleton_lock_prevents_two_senders(self):
        from app.worker import acquire_lock
        first=acquire_lock(self.store)
        try:
            with self.assertRaises(RuntimeError):acquire_lock(self.store)
        finally:first.close()

    def test_backup_restore_reverts_data_and_revokes_sessions(self):
        from app import manage
        self.save();session=self.login();self.shift();self.activate()
        mailer.queue_due(self.store,dt.datetime.combine(self.today,dt.time(10,10),TZ))
        with patch.object(manage,'ROOT',Path(self.tmp.name)):
            backup=manage.backup(self.store)
            self.save()
            with patch.dict(os.environ,{'DATABASE_PATH':self.store.path}),patch('sys.argv',['manage','restore','--source',str(backup),'--confirm-restoration']),patch('builtins.print'):
                manage.main()
        with self.store.read() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM registrations').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0],0)
            self.assertEqual(row(db,'SELECT status FROM outbox')['status'],'uncertain')

    def fixture(self,dates=None,names=None):
        dates=dates or [self.today+dt.timedelta(days=3)]
        header=['节次','一二节','签到','三四节','签到','五六节','签到','七八节','签到']
        content=[header]
        for date in dates:
            values=[date.isoformat()]
            for slot in range(4):values += [(names or ['张梓涵','李宥达','空','薛铖昊'])[slot],'']
            content.append(values)
        return workbook(['排班'],content,'签到表')

    def preview(self,data):
        with self.store.write() as db:return service.import_preview(db,self.admin,dict(file=base64.b64encode(data).decode()))

    def test_excel_import_preview_confirm_idempotent_and_empty_slot(self):
        fixture=self.fixture();p=self.preview(fixture)
        self.assertEqual(p['counts']['add'],3);self.assertEqual(p['empty'],1);self.assertEqual(p['errors'],[])
        with self.store.write() as db:
            service.import_confirm(db,self.admin,dict(id=p['id']));service.import_confirm(db,self.admin,dict(id=p['id']))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM shifts').fetchone()[0],3)
        second=self.preview(fixture);self.assertEqual(second['counts']['add'],0)
        with self.store.write() as db:service.import_confirm(db,self.admin,dict(id=second['id']))

    def test_import_unknown_name_duplicate_date_and_protected_record(self):
        p=self.preview(self.fixture(names=['陌生同学','李宥达','空','薛铖昊']))
        self.assertTrue(any('未知成员' in e for e in p['errors']))
        date=self.today+dt.timedelta(days=3);p=self.preview(self.fixture(dates=[date,date]))
        self.assertTrue(any('重复' in e for e in p['errors']))
        shift=self.shift();self.save(shift)
        p=self.preview(self.fixture(dates=[self.today],names=['薛铖昊','空','空','空']))
        self.assertTrue(any('已有登记' in e for e in p['errors']))

    def test_import_preserves_accepted_swap_when_source_unchanged(self):
        fixture=self.fixture();p=self.preview(fixture)
        with self.store.write() as db:
            service.import_confirm(db,self.admin,dict(id=p['id']))
            shift=row(db,'SELECT * FROM shifts WHERE slot=0')
            request=service.swap_create(db,self.user,dict(shift_id=shift['id'],version=1,recipient_id='m03'))
            service.swap_decide(db,self.other,dict(id=request['id'],decision='accept'))
        second=self.preview(fixture);self.assertEqual(second['errors'],[])
        with self.store.write() as db:
            service.import_confirm(db,self.admin,dict(id=second['id']))
            self.assertEqual(row(db,'SELECT member_id FROM shifts WHERE slot=0')['member_id'],'m03')

    def test_export_uses_same_filters_and_strings_never_formulas(self):
        self.save(tasks=[dict(text='=HYPERLINK("https://example.invalid")',status='done')])
        self.save(state='draft')
        with self.store.write() as db:
            db.execute('UPDATE members SET caps=? WHERE id=?',(js(['can_register','can_export']),'m02'))
            member=member_by_id(db,'m02')
            exported=service.export_records(db,member,dict(state='submitted',task_status='done'))
            self.assertEqual(len(service.state(db,member,dict(state='submitted',task_status='done'))['records']),1)
        z=zipfile.ZipFile(io.BytesIO(exported));root=ET.fromstring(z.read('xl/worksheets/sheet1.xml'))
        self.assertEqual(len(root.findall('s:sheetData/s:row',NS)),2)
        self.assertEqual(root.findall('.//s:f',NS),[])
        self.assertIn('=HYPERLINK',z.read('xl/worksheets/sheet1.xml').decode())

    def test_persistence_across_new_app_and_sessions(self):
        self.save();session=self.login()
        second=Store(self.store.path);second.init()
        reply=self.call('state',session=session['token'],app=App(second))
        self.assertEqual(reply['status'],200);self.assertEqual(len(reply['body']['records']),1)

    def test_otp_attempt_limit_and_single_use(self):
        email=self.user['email'];code='123456'
        with self.store.write() as db:db.execute('INSERT INTO auth_codes VALUES (?,?,?,?,0,0)',(email,digest(email+':'+code),(dt.datetime.now(TZ)+dt.timedelta(minutes=10)).isoformat(),now()))
        for _ in range(5):
            with self.assertRaises(Problem):auth.verify_code(self.store,dict(email=email,code='000000'))
        with self.assertRaises(Problem):auth.verify_code(self.store,dict(email=email,code=code))
        with self.store.write() as db:db.execute('UPDATE auth_codes SET attempts=0')
        self.assertTrue(auth.verify_code(self.store,dict(email=email,code=code))['token'])
        with self.assertRaises(Problem):auth.verify_code(self.store,dict(email=email,code=code))

    def test_ai_confirm_revalidates_permissions_and_cannot_spoof_author(self):
        with self.store.write() as db:
            db.execute("UPDATE settings SET value='true' WHERE key='ai_consent'")
            db.execute('INSERT INTO ai_proposals VALUES (?,?,?,?,?,NULL)',('p1','m02','records/save',js(dict(author_id='m01',date=str(self.today),state='submitted',tasks=[])),now()))
            with self.assertRaises(Problem):ai.confirm(db,self.user,dict(id='p1'))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM registrations').fetchone()[0],0)

    def test_two_sessions_see_same_persisted_record(self):
        first=self.login();second=self.login()
        saved=self.call('records/save',dict(date=str(self.today),state='submitted',tasks=[]),first['token'],first['csrf'])
        self.assertEqual(saved['status'],200)
        viewed=self.call('state',session=second['token'])
        self.assertEqual(viewed['body']['records'][0]['id'],saved['body']['id'])


if __name__=='__main__':unittest.main()
