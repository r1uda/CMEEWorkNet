import datetime as dt
import unittest
from unittest.mock import Mock

from app import mailer, public_swap, public_swap_mail, service
from app.core import Problem, TZ, js, member_by_id, row, rows
from tests.test_business import BusinessTests as Fixtures
from tests.test_public_swap import PublicSwapTests as SwapFixtures


class PublicSwapMailTests(unittest.TestCase):
    setUp = Fixtures.setUp
    tearDown = Fixtures.tearDown
    shift = Fixtures.shift
    create = SwapFixtures.create
    offer = SwapFixtures.offer

    def mode(self, value):
        with self.store.write() as db:
            db.execute('UPDATE settings SET value=? WHERE key=?', (js(value), 'public_swap_mail_mode'))
            for m in rows(db, 'SELECT id FROM members'):
                db.execute('UPDATE members SET email=? WHERE id=?', (m['id']+'@example.invalid', m['id']))

    def events(self, event):
        with self.store.read() as db:
            return rows(db, 'SELECT o.* FROM outbox o JOIN public_swap_mail_links l ON l.outbox_id=o.id WHERE l.event=?', (event,))

    def test_all_four_events_have_exact_audiences_and_deduplication(self):
        self.mode('all')
        post = self.create()
        self.assertEqual({m['recipient_id'] for m in self.events('published')}, {f'm{i:02}' for i in range(1,11)}-{'m02'})
        sender = Mock()
        mailer.process_outbox(self.store, sender=sender, limit=100)
        self.assertEqual(sender.call_count, 9)
        first = self.offer(post)
        second = self.offer(post, self.admin)
        self.assertEqual([m['recipient_id'] for m in self.events('offer')], ['m02', 'm02'])
        sender.reset_mock()
        mailer.process_outbox(self.store, sender=sender, limit=100)
        self.assertEqual(sender.call_count, 2)
        self.assertEqual({c.args[0] for c in sender.call_args_list}, {'m02@example.invalid'})
        with self.store.write() as db:
            public_swap.select(db, self.user, dict(offer_id=first))
            self.assertEqual(public_swap_mail.queue(db, post, 'selected', first), 0)
        self.assertEqual([m['recipient_id'] for m in self.events('selected')], ['m03'])
        self.assertEqual([m['recipient_id'] for m in self.events('not_selected')], ['m01'])
        sender.reset_mock()
        mailer.process_outbox(self.store, sender=sender, limit=100)
        self.assertEqual({c.args[0] for c in sender.call_args_list}, {'m03@example.invalid','m01@example.invalid'})
        mailer.process_outbox(self.store, sender=sender, limit=100)
        self.assertEqual(sender.call_count, 2)
        with self.store.read() as db:
            self.assertEqual(len(service.state(db,self.admin)['public_swaps'][0]['offers']), 2)

    def test_test_mode_only_two_people_even_when_other_notifications_paused(self):
        self.mode('test')
        with self.store.write() as db:
            for i in range(1,11):
                db.execute('INSERT INTO settings VALUES (?,?)', ('mail_notifications:'+f'm{i:02}', 'false'))
            self.user = member_by_id(db,'m03')
            self.other = member_by_id(db,'m10')
        origin = self.shift('m03',self.today+dt.timedelta(days=2))
        with self.store.write() as db:
            post = public_swap.create(db,self.user,dict(shift_id=origin,version=1))['id']
        self.assertEqual([m['recipient_id'] for m in self.events('published')], ['m10'])
        sender = Mock()
        mailer.process_outbox(self.store,sender=sender)
        self.assertEqual(sender.call_args.args[0],'m10@example.invalid')
        proposal = self.offer(post)
        sender.reset_mock()
        mailer.process_outbox(self.store,sender=sender)
        self.assertEqual(sender.call_args.args[0],'m03@example.invalid')
        with self.store.write() as db:
            public_swap.select(db,self.user,dict(offer_id=proposal))
        sender.reset_mock()
        mailer.process_outbox(self.store,sender=sender)
        self.assertEqual(sender.call_args.args[0],'m10@example.invalid')
        with self.store.read() as db:
            self.assertTrue(all(m['recipient_id'] in ('m03','m10') for m in rows(db,'SELECT recipient_id FROM outbox')))
            self.assertFalse(mailer.notifications_enabled(db,'m10'))

    def test_cancel_withdraw_and_scope_change_prevent_stale_mail(self):
        self.mode('all')
        post=self.create()
        proposal=self.offer(post)
        with self.store.write() as db:
            public_swap.withdraw(db,self.other,dict(id=proposal))
            public_swap.cancel(db,self.user,dict(id=post))
        sender=Mock()
        mailer.process_outbox(self.store,sender=sender,limit=100)
        self.assertEqual(sender.call_count,10)
        self.assertTrue(all('撤回' in c.args[1] or '取消' in c.args[1] for c in sender.call_args_list))
        sender.reset_mock()
        post=self.create()
        self.mode('test')
        mailer.process_outbox(self.store,sender=sender,limit=100)
        self.assertEqual({c.args[0] for c in sender.call_args_list},{'m03@example.invalid','m10@example.invalid'})

    def test_mail_scope_only_admin_and_off_does_not_queue_retroactively(self):
        with self.assertRaises(Problem), self.store.write() as db:
            service.settings_save(db,self.user,dict(public_swap_mail_mode='all'))
        self.create()
        self.assertEqual(self.events('published'),[])
        self.mode('all')
        self.assertEqual(self.events('published'),[])
        with self.assertRaises(Problem), self.store.write() as db:
            service.settings_save(db,self.admin,dict(public_swap_mail_mode='invalid'))
