import datetime as dt
import unittest

from app import public_swap, service
from app.core import Problem, member_by_id, row, rows
from tests.test_business import BusinessTests as Fixtures


class PublicSwapTests(unittest.TestCase):
    setUp = Fixtures.setUp
    tearDown = Fixtures.tearDown
    shift = Fixtures.shift
    save = Fixtures.save

    def create(self):
        self.created = getattr(self, 'created', 0) + 1
        self.origin = self.shift(date=self.today + dt.timedelta(days=self.created+1))
        with self.store.write() as db:
            return public_swap.create(db, self.user, dict(shift_id=self.origin, version=1, reason='需要换班'))['id']

    def offer(self, post, member=None, target=None):
        with self.store.write() as db:
            return public_swap.offer(db, member or self.other, dict(id=post, target_shift_id=target, target_version=1))['id']

    def test_two_offers_only_selected_one_changes_both_shifts_and_no_mail(self):
        post = self.create()
        target = self.shift('m03', self.today + dt.timedelta(days=3), 1)
        first = self.offer(post, target=target)
        second = self.offer(post, self.admin)
        with self.store.read() as db:
            self.assertEqual(row(db, 'SELECT member_id FROM shifts WHERE id=?', (self.origin,))['member_id'], 'm02')
            self.assertEqual(row(db, 'SELECT COUNT(*) AS n FROM outbox')['n'], 0)
        with self.store.write() as db:
            public_swap.select(db, self.user, dict(offer_id=first))
        with self.store.read() as db:
            a = row(db, 'SELECT * FROM shifts WHERE id=?', (self.origin,))
            b = row(db, 'SELECT * FROM shifts WHERE id=?', (target,))
            self.assertEqual((a['member_id'], a['original_member_id'], a['version']), ('m03', 'm02', 2))
            self.assertEqual((b['member_id'], b['original_member_id'], b['version']), ('m02', 'm03', 2))
            self.assertEqual(row(db, 'SELECT state FROM public_swap_offers WHERE id=?', (second,))['state'], 'not_selected')
            self.assertEqual(row(db, 'SELECT COUNT(*) AS n FROM outbox')['n'], 0)
            self.assertEqual(public_swap.listing(db, self.other)[0]['state'], 'matched')
        with self.assertRaises(Problem), self.store.write() as db:
            public_swap.select(db, self.user, dict(offer_id=second))
        self.store.init()
        with self.store.read() as db:
            self.assertFalse(rows(db, 'PRAGMA foreign_key_check'))

    def test_cover_and_private_history(self):
        post = self.create()
        proposal = self.offer(post)
        with self.store.write() as db:
            public_swap.select(db, self.user, dict(offer_id=proposal))
        with self.store.read() as db:
            self.assertEqual(service.state(db, self.other)['swaps'][0]['state'], 'accepted')
            self.assertIsNone(service.state(db, self.other)['swaps'][0]['target_shift_id'])

    def test_authorization_and_duplicate_offers(self):
        post = self.create()
        proposal = self.offer(post)
        for func, who, body in [
            (public_swap.offer, self.user, dict(id=post)),
            (public_swap.offer, self.other, dict(id=post)),
            (public_swap.select, self.other, dict(offer_id=proposal)),
            (public_swap.cancel, self.admin, dict(id=post)),
            (public_swap.withdraw, self.user, dict(id=proposal)),
            (public_swap.create, self.other, dict(shift_id=self.origin, version=1)),
            (public_swap.create, self.user, dict(shift_id=self.origin, version=1))]:
            with self.subTest(func=func.__name__, who=who['id']), self.assertRaises(Problem), self.store.write() as db:
                func(db, who, body)

    def test_withdraw_and_cancel(self):
        post = self.create()
        proposal = self.offer(post)
        with self.store.write() as db:
            public_swap.withdraw(db, self.other, dict(id=proposal))
        with self.assertRaises(Problem), self.store.write() as db:
            public_swap.select(db, self.user, dict(offer_id=proposal))
        self.offer(post)
        with self.store.write() as db:
            public_swap.cancel(db, self.user, dict(id=post))
        with self.assertRaises(Problem), self.store.write() as db:
            public_swap.offer(db, self.admin, dict(id=post))

    def test_target_changed_or_registered_rejects_whole_transaction(self):
        for mutation in ('version', 'registration'):
            with self.subTest(mutation=mutation):
                post = self.create()
                target = self.shift('m03', self.today + dt.timedelta(days=4), 2 if mutation=='version' else 3)
                proposal = self.offer(post, target=target)
                with self.store.write() as db:
                    if mutation == 'version':
                        db.execute('UPDATE shifts SET version=version+1 WHERE id=?', (target,))
                    else:
                        db.execute("INSERT INTO registrations(id,shift_id,author_id,date,slot,state,created_at,updated_at) VALUES ('test',?,'m03',?,3,'draft','test','test')", (target,str(self.today)))
                with self.assertRaises(Problem), self.store.write() as db:
                    public_swap.select(db, self.user, dict(offer_id=proposal))
                with self.store.read() as db:
                    self.assertEqual(row(db, 'SELECT member_id FROM shifts WHERE id=?', (self.origin,))['member_id'], 'm02')
                    self.assertEqual(row(db, 'SELECT COUNT(*) AS n FROM swaps')['n'], 0)
                with self.store.write() as db:
                    public_swap.cancel(db, self.user, dict(id=post))

    def test_origin_stale_and_direct_public_exclusivity(self):
        post = self.create()
        proposal = self.offer(post)
        with self.assertRaises(Problem), self.store.write() as db:
            service.swap_create(db, self.user, dict(shift_id=self.origin, version=1, recipient_id='m03'))
        with self.store.write() as db:
            db.execute('UPDATE shifts SET version=2 WHERE id=?', (self.origin,))
        with self.store.read() as db:
            self.assertEqual(public_swap.listing(db, self.user)[0]['state'], 'stale')
        with self.assertRaises(Problem), self.store.write() as db:
            public_swap.select(db, self.user, dict(offer_id=proposal))

    def test_concurrent_selection_has_exactly_one_winner(self):
        import threading
        post = self.create()
        offers = [self.offer(post), self.offer(post, self.admin)]
        results = []
        barrier = threading.Barrier(2)
        def choose(ident):
            barrier.wait()
            try:
                with self.store.write() as db:
                    public_swap.select(db, self.user, dict(offer_id=ident))
                results.append('accepted')
            except Problem:
                results.append('rejected')
        threads = [threading.Thread(target=choose, args=(ident,)) for ident in offers]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(sorted(results), ['accepted', 'rejected'])
        with self.store.read() as db:
            self.assertEqual(row(db, "SELECT COUNT(*) AS n FROM swaps WHERE state='accepted'")['n'], 1)
            self.assertEqual(row(db, "SELECT COUNT(*) AS n FROM public_swap_offers WHERE state='selected'")['n'], 1)


if __name__ == '__main__':
    unittest.main()
