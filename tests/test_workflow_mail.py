import datetime as dt
import os
import sqlite3
import unittest
from unittest.mock import Mock, patch

from app import handoff, mailer, service, workflow_mail, public_swap
from app.core import row, rows, Problem, js
from tests.test_swap_email import SwapEmailTests as Fixtures
from tests.test_public_swap_mail import PublicSwapMailTests as PublicFixtures


class WorkflowMailTests(unittest.TestCase):
    setUp = Fixtures.setUp
    tearDown = Fixtures.tearDown
    shift = Fixtures.shift
    request = Fixtures.request
    decide = Fixtures.decide

    def drain(self):
        sender = Mock()
        mailer.process_outbox(self.store, sender, self.at + dt.timedelta(minutes=1), limit=100)
        return [c.args for c in sender.call_args_list]

    def test_swap_request_and_result_ignore_old_reminder_pause_and_dedupe(self):
        with self.store.write() as db:
            for m in ('m02', 'm03'):
                db.execute('INSERT INTO settings VALUES (?,?)', ('mail_notifications:'+m, 'false'))
        w = self.request(self.shift())
        self.assertEqual([m[0] for m in self.drain()], ['m03@example.invalid'])
        self.decide(w['id'])
        with self.store.write() as db:
            workflow_mail.swap_result(db,w['id'])
        sent = self.drain()
        self.assertEqual([m[0] for m in sent], ['m02@example.invalid'])
        self.assertIn('https://workbench.example.invalid/#messages?tab=swaps&request='+w['id'], sent[0][2])
        self.assertEqual(self.drain(), [])

    def test_handoff_lifecycle_and_comments_notify_only_counterpart(self):
        with self.store.write() as db:
            h = handoff.create(db,self.requester,dict(recipient_id='m03',text='领取文件'))['id']
        sent = self.drain()
        self.assertEqual([m[0] for m in sent], ['m03@example.invalid'])
        self.assertIn('tab=handoffs&handoff='+h, sent[0][2])
        with self.store.write() as db:
            handoff.decide(db,self.recipient,dict(id=h,decision='accept'))
            handoff.comment(db,self.recipient,dict(id=h,text='收到 <script>bad</script>'))
            task = row(db,'SELECT direct_task_id FROM handoffs WHERE id=?',(h,))['direct_task_id']
            handoff.direct_progress(db,self.recipient,dict(task_id=task,text='已领取',status='done'))
        sent = self.drain()
        self.assertEqual(len(sent),3)
        self.assertEqual({m[0] for m in sent},{'m02@example.invalid'})
        with self.store.write() as db:
            handoff.comment(db,self.requester,dict(id=h,text='谢谢'))
        self.assertEqual([m[0] for m in self.drain()],['m03@example.invalid'])
        self.assertEqual(self.drain(),[])

    def test_handoff_reject_and_queue_failure_transaction(self):
        with self.store.write() as db:
            h=handoff.create(db,self.requester,dict(recipient_id='m03',text='测试'))['id']
            handoff.decide(db,self.recipient,dict(id=h,decision='reject'))
        sent=self.drain()
        self.assertEqual(len(sent),2)
        self.assertEqual({m[0] for m in sent},{'m02@example.invalid','m03@example.invalid'})
        with self.store.write() as db:
            db.execute("CREATE TRIGGER fail_workflow BEFORE INSERT ON outbox WHEN NEW.kind='workflow' BEGIN SELECT RAISE(ABORT,'fail'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.write() as db:
                handoff.create(db,self.requester,dict(recipient_id='m03',text='must rollback'))
        with self.store.read() as db:
            self.assertEqual(len(rows(db,'SELECT * FROM handoffs')),1)

    def test_html_email_has_clickable_url_and_escapes_content(self):
        client=Mock()
        client.send_message.return_value={}
        with patch.dict(os.environ,{'SMTP_HOST':'smtp.example.invalid','MAIL_FROM':'sender@example.invalid','SMTP_USER':'','SMTP_SECURITY':'ssl'}), patch('app.mailer.smtplib.SMTP_SSL',return_value=client):
            mailer.send_email('recipient@example.invalid','通知','<script>alert(1)</script>\nhttps://workbench.example.invalid/#messages?tab=handoffs&handoff=abc')
        msg=client.send_message.call_args.args[0]
        html=msg.get_body(preferencelist=('html',)).get_content()
        self.assertNotIn('<script>',html)
        self.assertIn('&lt;script&gt;',html)
        self.assertIn('<a href="https://workbench.example.invalid/#messages?tab=handoffs&amp;handoff=abc">',html)
        self.assertIn('&handoff=abc',msg.get_body(preferencelist=('plain',)).get_content())


class PublicExtraMailTests(unittest.TestCase):
    setUp=PublicFixtures.setUp
    tearDown=PublicFixtures.tearDown
    shift=PublicFixtures.shift
    create=PublicFixtures.create
    offer=PublicFixtures.offer
    mode=PublicFixtures.mode

    def test_withdraw_only_requester_cancel_all_others(self):
        self.mode('all')
        p=self.create()
        o=self.offer(p)
        with self.store.write() as db:
            public_swap.withdraw(db,self.other,dict(id=o))
            public_swap.cancel(db,self.user,dict(id=p))
            events=rows(db,'SELECT o.recipient_id,l.event FROM outbox o JOIN workflow_mail_links l ON l.outbox_id=o.id')
        self.assertEqual([e['recipient_id'] for e in events if e['event']=='withdrawn'],['m02'])
        self.assertEqual({e['recipient_id'] for e in events if e['event']=='canceled'},{f'm{i:02}' for i in range(1,11)}-{'m02'})
