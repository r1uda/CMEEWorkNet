import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import escalation_mail, mailer, service
from app.core import CAPS, TZ, Store, js, member_by_id, row, rows


class EscalationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.store=Store(Path(self.tmp.name)/'test.sqlite3');self.store.init()
        self.at=dt.datetime(2026,10,29,9,tzinfo=TZ)
        with self.store.write() as db:
            for mid in ['m03','m09','m10']:
                db.execute('UPDATE members SET caps=?,email=? WHERE id=?',(js(CAPS),mid+'@example.invalid',mid))
            db.execute("UPDATE settings SET value=? WHERE key='activation_date'",(js('2026-10-28'),))
            db.execute("UPDATE settings SET value=? WHERE key='escalation_recipient'",(js('m10'),))
            self.admin=member_by_id(db,'m03');self.person=member_by_id(db,'m02')

    def tearDown(self):self.tmp.cleanup()

    def shift(self,sid,slot=0,date='2026-10-28'):
        with self.store.write() as db:db.execute('INSERT INTO shifts(id,date,slot,original_member_id,member_id) VALUES (?,?,?,?,?)',(sid,date,slot,'m01','m02'))

    def test_ten_minute_reminder_next_day_nine_and_only_designated_recipient(self):
        self.shift('s')
        self.assertEqual(mailer.queue_due(self.store,dt.datetime(2026,10,28,10,9,tzinfo=TZ)),0)
        self.assertEqual(mailer.queue_due(self.store,dt.datetime(2026,10,28,10,10,tzinfo=TZ)),1)
        self.assertEqual(escalation_mail.queue_due(self.store,self.at.replace(hour=8,minute=59)),0)
        self.assertEqual(escalation_mail.queue_due(self.store,self.at),1)
        self.assertEqual(escalation_mail.queue_due(self.store,self.at),0)
        sent=[]
        mailer.process_outbox(self.store,sender=lambda *a:sent.append(a),at=self.at)
        self.assertEqual(len(sent),1);self.assertEqual(sent[0][0],'m10@example.invalid')
        self.assertIn('张梓涵',sent[0][2]);self.assertIn('本人邮箱未绑定',sent[0][2])
        self.assertNotIn('薛铖昊',sent[0][2]);self.assertNotIn('AI 处理建议',sent[0][2])
        self.assertEqual(escalation_mail.queue_due(self.store,self.at+dt.timedelta(days=1)),0)

    def test_send_time_filters_submitted_and_ignores_history_future_and_canceled(self):
        for sid,slot in [('done',0),('draft',1),('missing',2),('canceled',3)]:self.shift(sid,slot)
        self.shift('old',0,'2026-10-27');self.shift('today',0,'2026-10-29')
        with self.store.write() as db:
            db.execute("UPDATE shifts SET active=0 WHERE id='canceled'")
            service.save_record(db,self.person,dict(shift_id='draft',shift_version=1,state='draft',tasks=[]))
        escalation_mail.queue_due(self.store,self.at)
        with self.store.write() as db:service.save_record(db,self.person,dict(shift_id='done',shift_version=1,state='submitted',tasks=[]))
        sent=[];mailer.process_outbox(self.store,sender=lambda *a:sent.append(a),at=self.at)
        self.assertEqual(len(sent),1)
        self.assertIn('共 2 个班次',sent[0][2]);self.assertIn('草稿未提交',sent[0][2])
        self.assertNotIn('一二节',sent[0][2]);self.assertNotIn('2026-10-27',sent[0][2])
        with self.store.read() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM reminder_escalation_items').fetchone()[0],2)

    def test_all_submitted_or_recipient_demoted_cancels_without_send(self):
        self.shift('s');escalation_mail.queue_due(self.store,self.at)
        with self.store.write() as db:db.execute("UPDATE members SET caps='[\"can_register\"]' WHERE id='m10'")
        sent=[];mailer.process_outbox(self.store,sender=lambda *a:sent.append(a),at=self.at)
        self.assertEqual(sent,[])
        with self.store.read() as db:self.assertEqual(row(db,"SELECT status FROM outbox WHERE kind='reminder_escalation'")['status'],'canceled')
        with self.store.write() as db:
            db.execute('UPDATE members SET caps=? WHERE id=?',(js(CAPS),'m10'))
        escalation_mail.queue_due(self.store,self.at)
        with self.store.write() as db:service.save_record(db,self.person,dict(shift_id='s',shift_version=1,state='submitted',tasks=[]))
        mailer.process_outbox(self.store,sender=lambda *a:sent.append(a),at=self.at)
        self.assertEqual(sent,[])

    def test_actual_ai_advice_is_labelled_and_missing_ai_has_safe_fallback(self):
        self.shift('s');escalation_mail.queue_due(self.store,self.at)
        with patch('app.escalation_mail.ai_advice',return_value='请核实情况并提醒补登记。'):
            sent=[];mailer.process_outbox(self.store,sender=lambda *a:sent.append(a),at=self.at)
        self.assertIn('AI 处理建议（供参考）：请核实情况并提醒补登记。',sent[0][2])

    def test_retry_does_not_duplicate_and_does_not_run_before_nine(self):
        self.shift('s');escalation_mail.queue_due(self.store,self.at)
        def fail(*args):raise OSError('test')
        mailer.process_outbox(self.store,sender=fail,at=self.at)
        sent=[];mailer.process_outbox(self.store,sender=lambda *a:sent.append(a),at=self.at+dt.timedelta(minutes=3))
        self.assertEqual(len(sent),1)
        mailer.process_outbox(self.store,sender=lambda *a:sent.append(a),at=self.at+dt.timedelta(minutes=4))
        self.assertEqual(len(sent),1)


if __name__=='__main__':unittest.main()
