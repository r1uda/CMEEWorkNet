import datetime as dt
import unittest
from unittest.mock import Mock
from app import handoff, mailer, notifications, public_swap, public_swap_mail, service
from app.core import CAPS, Problem, js, member_by_id, row, rows
from tests.test_public_swap_mail import PublicSwapMailTests as Fixtures


class NoticePolishTests(unittest.TestCase):
    setUp=Fixtures.setUp
    tearDown=Fixtures.tearDown
    shift=Fixtures.shift
    mode=Fixtures.mode
    create=Fixtures.create
    offer=Fixtures.offer

    def test_specific_post_sends_only_two_including_author_without_changing_global(self):
        self.mode('all')
        shift=self.shift(member='m01',date=self.today+dt.timedelta(days=2))
        with self.store.write() as db:
            p=public_swap.create(db,self.admin,dict(shift_id=shift,version=1,mail_audience=['m01','m03']))['id']
            mails=rows(db,'SELECT recipient_id FROM outbox')
            self.assertEqual({m['recipient_id'] for m in mails},{'m01','m03'})
            self.assertTrue(public_swap_mail.enabled(db,'m02'))
            self.assertFalse(public_swap_mail.enabled(db,'m02',p))
        sender=Mock()
        mailer.process_outbox(self.store,sender=sender)
        self.assertEqual({c.args[0] for c in sender.call_args_list},{'m01@example.invalid','m03@example.invalid'})
        with self.store.write() as db:
            public_swap.cancel(db,self.admin,dict(id=p))
            canceled=rows(db,"SELECT recipient_id FROM outbox WHERE kind='workflow'")
            self.assertEqual([m['recipient_id'] for m in canceled],['m03'])

    def test_scope_requires_admin(self):
        shift=self.shift(date=self.today+dt.timedelta(days=2))
        with self.assertRaises(Problem),self.store.write() as db:
            public_swap.create(db,self.user,dict(shift_id=shift,version=1,mail_audience=['m02']))

    def test_seen_is_per_recipient_and_new_event_becomes_unread(self):
        with self.store.write() as db:
            notifications.emit(db,'m01',['m02','m03'],'first','内容','#messages?tab=handoffs')
            notifications.emit(db,'m01',['m02','m03'],'first','内容','#messages?tab=handoffs')
            m=service.state(db,self.user)['messages'][0]
            self.assertIsNone(m['read_at'])
            self.assertEqual(m['target_url'],'#messages?tab=handoffs')
            service.messages_seen(db,self.user,dict(ids=[m['id'],'unknown']))
            self.assertIsNotNone(service.state(db,self.user)['messages'][0]['read_at'])
            self.assertIsNone(service.state(db,self.other)['messages'][0]['read_at'])
            notifications.emit(db,'m01',['m02'],'second','新内容','#messages?tab=swaps')
            self.assertEqual(sum(not x['read_at'] for x in service.state(db,self.user)['messages']),1)
            self.assertEqual(len(rows(db,'SELECT * FROM messages')),2)

    def test_blank_progress_changes_status_and_notifies_other_party(self):
        with self.store.write() as db:
            h=handoff.create(db,self.user,dict(recipient_id='m03',text='测试事项'))['id']
            handoff.decide(db,self.other,dict(id=h,decision='accept'))
            task=row(db,'SELECT direct_task_id FROM handoffs WHERE id=?',(h,))['direct_task_id']
            handoff.direct_progress(db,self.other,dict(task_id=task,status='done',text=''))
            self.assertEqual(row(db,'SELECT status FROM direct_tasks WHERE id=?',(task,))['status'],'done')
            self.assertEqual(row(db,'SELECT text FROM direct_progress WHERE task_id=?',(task,))['text'],'')
            self.assertTrue(any('事务已完成' in m['body'] for m in service.state(db,self.user)['messages']))
