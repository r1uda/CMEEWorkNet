"""Swap requests, notification mail and actual scheduling stay consistent."""
import contextlib
import datetime as dt
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app import mailer, service
from app.core import CAPS, ROOT, TZ, Store, js, member_by_id, now, row, rows, uid


class SwapEmailTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='swap-mail-', dir=Path(__file__).resolve().parent)
        self.store = Store(Path(self.tmp.name) / 'test.sqlite3')
        self.store.init()
        self.at = dt.datetime.now(TZ) + dt.timedelta(seconds=2)
        self.date = (self.at.date() + dt.timedelta(days=2)).isoformat()
        self.env = patch.dict(os.environ, {'APP_URL': 'https://workbench.example.invalid'})
        self.env.start()
        with self.store.write() as db:
            db.execute('UPDATE members SET caps=? WHERE id=?', (js(CAPS), 'm01'))
            for ident in ('m01', 'm02', 'm03'):
                db.execute('UPDATE members SET email=? WHERE id=?', (ident + '@example.invalid', ident))
            self.admin = member_by_id(db, 'm01')
            self.requester = member_by_id(db, 'm02')
            self.recipient = member_by_id(db, 'm03')

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def shift(self, member='m02', slot=0):
        ident = uid()
        with self.store.write() as db:
            db.execute('INSERT INTO shifts(id,date,slot,original_member_id,member_id) VALUES (?,?,?,?,?)',
                       (ident, self.date, slot, member, member))
        return ident

    def request(self, shift, target=None):
        with self.store.write() as db:
            return service.swap_create(db, self.requester, dict(
                shift_id=shift, version=1, recipient_id='m03',
                target_shift_id=target, target_version=1 if target else None,
                reason='请帮忙代班；这是独立测试数据'))

    def mail(self, request_id):
        with self.store.read() as db:
            return row(db, '''SELECT o.* FROM outbox o JOIN swap_mail_links l ON l.outbox_id=o.id
                              WHERE l.swap_id=?''', (request_id,))

    def decide(self, request_id, decision='accept'):
        with self.store.write() as db:
            return service.swap_decide(db, self.recipient, dict(id=request_id, decision=decision))

    def test_browser_single_cover_empty_target_is_null_and_queues_mail(self):
        shift=self.shift()
        with self.store.write() as db:
            result=service.swap_create(db,self.requester,dict(shift_id=shift,version=1,recipient_id='m03',target_shift_id='',reason='浏览器单次代班'))
            saved=row(db,'SELECT * FROM swaps WHERE id=?',(result['id'],))
            self.assertIsNone(saved['target_shift_id'])
        self.assertEqual(self.mail(result['id'])['status'],'pending')
        self.assertEqual(self.decide(result['id'])['state'],'accepted')

    def test_request_atomically_queues_recipient_email_without_reminder_activation(self):
        shift = self.shift()
        request = self.request(shift)
        mail = self.mail(request['id'])
        self.assertEqual(mail['kind'], 'swap_request')
        self.assertEqual(mail['dedupe_key'], 'swap-request:' + request['id'])
        self.assertEqual(mail['recipient_id'], 'm03')
        self.assertIsNone(mail['shift_id'])
        self.assertEqual(mail['status'], 'pending')
        with self.store.read() as db:
            self.assertEqual(row(db, 'SELECT member_id FROM shifts WHERE id=?', (shift,))['member_id'], 'm02')
        sent = []
        mailer.process_outbox(self.store, lambda *args: sent.append(args), self.at)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], 'm03@example.invalid')
        self.assertIn(self.date, sent[0][2])
        self.assertIn('张梓涵', sent[0][2])
        self.assertIn('tab=swaps', sent[0][2])
        self.assertNotIn('token=', sent[0][2])

    def test_queue_failure_rolls_back_the_request_too(self):
        shift = self.shift()
        with self.store.write() as db:
            db.execute("""CREATE TRIGGER fail_swap_email BEFORE INSERT ON outbox
                          WHEN NEW.kind='swap_request' BEGIN SELECT RAISE(ABORT,'test queue failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.request(shift)
        with self.store.read() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM swaps').fetchone()[0], 0)

    def test_repeated_worker_does_not_repeat_request_email(self):
        request = self.request(self.shift())
        sent = []
        for _ in range(3):
            mailer.process_outbox(self.store, lambda *args: sent.append(args), self.at)
        self.assertEqual(len(sent), 1)
        self.assertEqual(self.mail(request['id'])['status'], 'sent')

    def test_known_failure_retries_same_request_email(self):
        request = self.request(self.shift())
        def fail(*args):
            raise RuntimeError('simulated SMTP connection failure')
        mailer.process_outbox(self.store, fail, self.at)
        self.assertEqual(self.mail(request['id'])['status'], 'failed')
        sent = []
        mailer.process_outbox(self.store, lambda *args: sent.append(args), self.at + dt.timedelta(minutes=3))
        self.assertEqual(len(sent), 1)
        self.assertEqual(self.mail(request['id'])['attempts'], 2)

    def test_uncertain_request_delivery_is_not_automatically_retried(self):
        request = self.request(self.shift())
        attempts = []
        def uncertain(*args):
            attempts.append(args)
            raise mailer.UncertainDelivery('simulated lost SMTP response')
        mailer.process_outbox(self.store, uncertain, self.at)
        mailer.process_outbox(self.store, uncertain, self.at + dt.timedelta(hours=1))
        self.assertEqual(len(attempts), 1)
        self.assertEqual(self.mail(request['id'])['status'], 'uncertain')

    def test_accepted_request_cancels_unsent_invitation(self):
        request = self.request(self.shift())
        self.decide(request['id'])
        sent = []
        mailer.process_outbox(self.store, lambda *args: sent.append(args), self.at)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], 'm02@example.invalid')
        self.assertIn('同意', sent[0][1])
        self.assertEqual(self.mail(request['id'])['status'], 'canceled')

    def test_rejected_request_cancels_unsent_invitation(self):
        request = self.request(self.shift())
        self.decide(request['id'], 'reject')
        sent = []
        mailer.process_outbox(self.store, lambda *args: sent.append(args), self.at)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], 'm02@example.invalid')
        self.assertIn('拒绝', sent[0][1])
        self.assertEqual(self.mail(request['id'])['status'], 'canceled')

    def test_expired_request_is_not_emailed(self):
        request = self.request(self.shift())
        after_shift = dt.datetime.fromisoformat(self.date + 'T10:01:00+08:00')
        sent = []
        mailer.process_outbox(self.store, lambda *args: sent.append(args), after_shift)
        self.assertEqual(sent, [])
        self.assertEqual(self.mail(request['id'])['status'], 'canceled')

    def test_slow_earlier_delivery_does_not_send_a_later_expired_invitation(self):
        first = self.request(self.shift(slot=1))  # Ends at noon, still valid after the slow send.
        later = self.request(self.shift(slot=0))  # Ends at 10:00 while the first email is sent.
        first_mail, later_mail = self.mail(first['id']), self.mail(later['id'])
        before = dt.datetime.fromisoformat(self.date + 'T09:59:55+08:00')
        after = dt.datetime.fromisoformat(self.date + 'T10:00:05+08:00')
        clock = [before]

        class AdvancingDateTime(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)

        with self.store.write() as db:
            for index, item in enumerate((first_mail, later_mail)):
                stamp = (before - dt.timedelta(minutes=2-index)).isoformat(timespec='seconds')
                db.execute('UPDATE outbox SET created_at=?,next_attempt_at=? WHERE id=?',
                           (stamp, stamp, item['id']))
        sent = []

        def slow_sender(*args):
            sent.append(args)
            clock[0] = after

        # Do not pass `at`: exercise the production path that must advance its clock per job.
        with patch.object(mailer, 'dt', SimpleNamespace(datetime=AdvancingDateTime, timedelta=dt.timedelta)):
            mailer.process_outbox(self.store, slow_sender)
        self.assertEqual(len(sent), 1)
        self.assertEqual(self.mail(first['id'])['status'], 'sent')
        self.assertEqual(self.mail(later['id'])['status'], 'canceled')

    def test_changed_source_ownership_or_version_cancels_invitation(self):
        shift = self.shift()
        request = self.request(shift)
        with self.store.write() as db:
            db.execute('UPDATE shifts SET member_id=?,version=version+1 WHERE id=?', ('m01', shift))
        sent = []
        mailer.process_outbox(self.store, lambda *args: sent.append(args), self.at)
        self.assertEqual(sent, [])
        self.assertEqual(self.mail(request['id'])['status'], 'canceled')

    def test_changed_exchange_target_cancels_invitation(self):
        source, target = self.shift(), self.shift('m03', 1)
        request = self.request(source, target)
        with self.store.write() as db:
            db.execute('UPDATE shifts SET version=version+1 WHERE id=?', (target,))
        sent = []
        mailer.process_outbox(self.store, lambda *args: sent.append(args), self.at)
        self.assertEqual(sent, [])
        self.assertEqual(self.mail(request['id'])['status'], 'canceled')

    def test_registration_after_request_cancels_invitation(self):
        shift = self.shift()
        request = self.request(shift)
        # A saved draft on either involved shift makes a previously requested transfer invalid.
        with self.store.write() as db:
            db.execute('''INSERT INTO registrations(id,shift_id,author_id,date,slot,state,notes,version,created_at,updated_at)
                          VALUES (?,?,?,?,?,'draft','',1,?,?)''',
                       (uid(), shift, 'm02', self.date, 0, now(), now()))
        sent = []
        mailer.process_outbox(self.store, lambda *args: sent.append(args), self.at)
        self.assertEqual(sent, [])
        self.assertEqual(self.mail(request['id'])['status'], 'canceled')

    def test_disabling_reminders_does_not_cancel_request_email(self):
        request = self.request(self.shift())
        with self.store.write() as db:
            service.settings_save(db, self.admin, dict(activation_date=''))
        self.assertEqual(self.mail(request['id'])['status'], 'pending')
        sent = []
        mailer.process_outbox(self.store, lambda *args: sent.append(args), self.at)
        self.assertEqual(len(sent), 1)

    def test_acceptance_updates_both_timetables_and_reminders_after_request_mail(self):
        source, target = self.shift(), self.shift('m03', 1)
        request = self.request(source, target)
        invitations = []
        mailer.process_outbox(self.store, lambda *args: invitations.append(args), self.at)
        self.assertEqual(len(invitations), 1)
        self.decide(request['id'])
        results = []
        mailer.process_outbox(self.store, lambda *args: results.append(args), self.at)
        self.assertEqual(len(results), 1)
        with self.store.write() as db:
            requester = service.state(db, self.requester)
            recipient = service.state(db, self.recipient)
            self.assertEqual([(s['id'], s['member_id']) for s in requester['shifts']], [(target, 'm02')])
            self.assertEqual([(s['id'], s['member_id']) for s in recipient['shifts']], [(source, 'm03')])
            db.execute('UPDATE settings SET value=? WHERE key=?', (js(self.date), 'activation_date'))
        due = dt.datetime.fromisoformat(self.date + 'T12:10:00+08:00')
        self.assertEqual(mailer.queue_due(self.store, due), 2)
        reminders = []
        mailer.process_outbox(self.store, lambda *args: reminders.append(args), due)
        self.assertCountEqual([mail[0] for mail in reminders], ['m02@example.invalid', 'm03@example.invalid'])
        self.assertEqual(len(reminders), 2)
        with self.store.read() as db:
            self.assertEqual(row(db, 'SELECT original_member_id FROM shifts WHERE id=?', (source,))['original_member_id'], 'm02')
            self.assertEqual(row(db, 'SELECT original_member_id FROM shifts WHERE id=?', (target,))['original_member_id'], 'm03')

    def test_state_exposes_queued_mail_without_claiming_delivery_when_unconfigured(self):
        with self.store.write() as db:
            db.execute('UPDATE members SET email=NULL WHERE id=?', ('m03',))
        with patch.dict(os.environ, {'SMTP_HOST': '', 'MAIL_FROM': ''}):
            request = self.request(self.shift())
            mailer.tick(self.store)
        with self.store.read() as db:
            visible = service.state(db, self.requester)['swaps'][0]
            self.assertEqual(visible['id'], request['id'])
            self.assertEqual(visible['email_status'], 'pending')
            self.assertIn('email_error', visible)
        self.assertIsNone(self.mail(request['id'])['sent_at'])

    def test_missing_email_waits_without_consuming_retries_and_uses_later_binding(self):
        request = self.request(self.shift())
        with self.store.write() as db:
            db.execute('UPDATE members SET email=NULL WHERE id=?', ('m03',))
        sent = []
        mailer.process_outbox(self.store, lambda *args: sent.append(args), self.at)
        waiting = self.mail(request['id'])
        self.assertEqual(sent, [])
        self.assertEqual(waiting['status'], 'pending')
        self.assertEqual(waiting['attempts'], 0)
        self.assertIn('邮箱', waiting['error'])
        with self.store.write() as db:
            db.execute('UPDATE members SET email=? WHERE id=?', ('new-binding@example.invalid', 'm03'))
        mailer.process_outbox(self.store, lambda *args: sent.append(args), dt.datetime.fromisoformat(waiting['next_attempt_at']))
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], 'new-binding@example.invalid')
        self.assertEqual(self.mail(request['id'])['status'], 'sent')

    def test_v1_database_migrates_without_losing_members_or_records(self):
        legacy = Store(Path(self.tmp.name) / 'legacy.sqlite3')
        with contextlib.closing(sqlite3.connect(legacy.path)) as db:
            db.executescript((ROOT / 'app/schema.sql').read_text('utf-8'))
            db.execute('INSERT INTO members(id,name,email) VALUES (?,?,?)', ('m02', '张梓涵', 'kept@example.invalid'))
            db.execute('''INSERT INTO registrations(id,author_id,date,state,notes,created_at,updated_at)
                          VALUES ('kept','m02',?,'submitted','保留原始登记',?,?)''', (self.date, now(), now()))
            db.commit()
        legacy.init()
        legacy.init()
        with legacy.read() as db:
            self.assertGreaterEqual(db.execute('SELECT MAX(version) FROM schema_version').fetchone()[0], 2)
            self.assertIsNotNone(row(db, "SELECT name FROM sqlite_master WHERE type='table' AND name='swap_mail_links'"))
            self.assertEqual(row(db, "SELECT notes FROM registrations WHERE id='kept'")['notes'], '保留原始登记')
            self.assertEqual(row(db, "SELECT email FROM members WHERE id='m02'")['email'], 'kept@example.invalid')


if __name__ == '__main__':
    unittest.main()
