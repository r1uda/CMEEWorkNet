import datetime as dt
import unittest
from unittest.mock import Mock

from app import public_swap, mailer
from app.core import Problem, row, rows
from tests.test_public_swap_mail import PublicSwapMailTests


class PublicReplyTests(unittest.TestCase):
    setUp = PublicSwapMailTests.setUp
    tearDown = PublicSwapMailTests.tearDown
    shift = PublicSwapMailTests.shift
    create = PublicSwapMailTests.create
    mode = PublicSwapMailTests.mode
    events = PublicSwapMailTests.events

    def reply(self, post, kind, **kw):
        with self.store.write() as db:
            return public_swap.offer(db, self.other, dict(id=post, reply_kind=kind, **kw))['id']

    def test_undecided_is_visible_and_emails_only_requester_without_allowing_selection(self):
        self.mode('all'); post = self.create()
        ident = self.reply(post, 'undecided')
        mails = self.events('offer')
        self.assertEqual([x['recipient_id'] for x in mails], ['m02'])
        self.assertIn('具体班次待定', mails[0]['subject'])
        self.assertIn('不代表同意直接代班', mails[0]['body'])
        self.assertIn('#messages?tab=swaps&view=public&public='+post, mails[0]['body'])
        with self.store.read() as db:
            visible = public_swap.listing(db, self.admin)[0]['offers'][0]
            self.assertEqual(visible['reply_kind'], 'undecided')
            self.assertEqual(row(db, 'SELECT member_id FROM shifts WHERE id=?',(self.origin,))['member_id'], 'm02')
        with self.assertRaises(Problem), self.store.write() as db:
            public_swap.select(db, self.user, {'offer_id':ident})
        with self.store.read() as db:
            self.assertEqual(row(db,'SELECT COUNT(*) AS n FROM swaps')['n'],0)
            self.assertEqual(row(db,'SELECT state FROM public_swaps WHERE id=?',(post,))['state'],'open')

    def test_later_choose_shift_sends_new_specific_mail_and_preserves_single_active_reply(self):
        self.mode('all'); post = self.create()
        first = self.reply(post, 'undecided')
        date = self.today+dt.timedelta(days=4)
        target = self.shift('m03',date,2)
        second = self.reply(post,'exchange',replace_offer_id=first,target_shift_id=target,target_version=1)
        with self.store.read() as db:
            offers = rows(db,"SELECT * FROM public_swap_offers WHERE state='active'")
            self.assertEqual([o['id'] for o in offers],[second])
            self.assertEqual(row(db,'SELECT state FROM public_swap_offers WHERE id=?',(first,))['state'],'withdrawn')
        email = self.events('offer')[-1]
        self.assertIn(str(date), email['body'])
        self.assertIn('五六节', email['body'])
        self.assertIn('14:00—16:00', email['body'])
        self.assertIn('你的班次：', email['body'])
        sender=Mock();mailer.process_outbox(self.store,sender=sender,limit=100)
        # Publication to nine members; only the latest reply is sent to its requester.
        requester_mails=[c for c in sender.call_args_list if c.args[0]=='m02@example.invalid']
        self.assertEqual(len(requester_mails),1)
        self.assertIn(str(date),requester_mails[0].args[2])
        with self.store.write() as db:
            public_swap.select(db,self.user,{'offer_id':second})
        with self.store.read() as db:
            self.assertEqual(row(db,'SELECT member_id FROM shifts WHERE id=?',(target,))['member_id'],'m02')
            self.assertEqual(row(db,'SELECT member_id FROM shifts WHERE id=?',(self.origin,))['member_id'],'m03')

    def test_later_explicit_cover_is_not_the_same_as_undecided(self):
        post=self.create(); first=self.reply(post,'undecided')
        cover=self.reply(post,'cover',replace_offer_id=first)
        with self.store.write() as db:
            public_swap.select(db,self.user,{'offer_id':cover})
        with self.store.read() as db:
            self.assertEqual(row(db,'SELECT member_id FROM shifts WHERE id=?',(self.origin,))['member_id'],'m03')

    def test_invalid_edit_and_race_do_not_remove_previous_reply_or_send_extra_mail(self):
        self.mode('all');post=self.create(); first=self.reply(post,'undecided')
        bad=[dict(reply_kind='exchange'),dict(reply_kind='cover',target_shift_id=self.origin,target_version=1),
             dict(reply_kind='undecided',target_shift_id=self.origin,target_version=1),
             dict(reply_kind='invalid'),dict(reply_kind='undecided',message=None)]
        for b in bad:
            with self.subTest(body=b),self.assertRaises(Problem),self.store.write() as db:
                public_swap.offer(db,self.other,dict(id=post,replace_offer_id=first,**b))
        with self.assertRaises(Problem):self.reply(post,'undecided',replace_offer_id=first)
        second=self.reply(post,'cover',replace_offer_id=first)
        with self.assertRaises(Problem):self.reply(post,'undecided',replace_offer_id=first)
        with self.store.read() as db:
            self.assertEqual([o['id'] for o in rows(db,"SELECT id FROM public_swap_offers WHERE state='active'")],[second])
            self.assertEqual(len(self.events('offer')),2)

    def test_other_member_cannot_replace_reply(self):
        post=self.create();first=self.reply(post,'undecided')
        with self.assertRaises(Problem),self.store.write() as db:
            public_swap.offer(db,self.admin,dict(id=post,reply_kind='cover',replace_offer_id=first))


if __name__=='__main__':unittest.main()
