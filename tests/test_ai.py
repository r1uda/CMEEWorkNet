import datetime as dt
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

from app import ai, service
from app.core import Store, TZ, Problem, js, member_by_id, row


class AITests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='ai-',dir=Path(__file__).resolve().parent)
        self.store=Store(Path(self.tmp.name)/'db.sqlite3');self.store.init()
        with self.store.write() as db:
            db.execute("UPDATE settings SET value='true' WHERE key='ai_consent'")
            self.member=member_by_id(db,'m02');other=member_by_id(db,'m03')
            self.date=dt.datetime.now(TZ).date().isoformat()
            self.own=service.save_record(db,self.member,dict(date=self.date,state='submitted',tasks=[dict(text='本人记录',status='pending')]))
            service.save_record(db,other,dict(date=self.date,state='submitted',tasks=[dict(text='其他成员的私有内容',status='pending')]))
            db.execute("UPDATE settings SET value='true' WHERE key='share_records'")
        self.env=patch.dict(os.environ,{'AI_BASE_URL':'https://model.example.invalid/v1','AI_MODEL':'test-only','AI_API_KEY':'test-only-not-a-secret','AI_DAILY_LIMIT':'30'})
        self.env.start();self.requests=[]

    def tearDown(self):self.env.stop();self.tmp.cleanup()

    def response(self,output):
        def open_request(req,timeout):
            self.requests.append(json.loads(req.data))
            return io.BytesIO(js(dict(choices=[dict(message=dict(content=js(output)))])).encode())
        return patch('urllib.request.urlopen',side_effect=open_request)

    def test_full_history_context_and_pagination_include_old_records(self):
        with self.store.write() as db:
            for i in range(105):
                service.save_record(db,self.member,dict(date='2025-01-01',state='submitted',tasks=[dict(text=f'历史事项{i}',status='done')]))
        with self.response(dict(message='全部历史',sources=[],action=None)):
            ai.ask(self.store,self.member,dict(message='整理全部事务记录',consent=True))
        content=self.requests[0]['messages'][-1]['content']
        context=json.loads(content.split('当前授权上下文：',1)[1].split('\n用户输入：',1)[0])
        self.assertEqual(context['history_coverage']['total_records'],106)
        self.assertTrue(context['history_coverage']['complete'])
        self.assertEqual(context['history_summary']['from_date'],'2025-01-01')
        self.assertEqual(len(context['records']),106)
        self.assertEqual(context['history_summary']['submitted_records'],106)
        self.assertEqual(context['history_summary']['draft_records'],0)
        self.assertNotIn('其他成员的私有内容',js(context))
        with self.store.read() as db:
            first=ai.query_data(db,self.member,dict(kind='records'))
            second=ai.query_data(db,self.member,dict(kind='records',offset=first['next_offset']))
        self.assertEqual(first['match_count'],106);self.assertEqual(len(first['records']),100)
        self.assertEqual(len(second['records']),6);self.assertFalse(second['has_more'])
        self.assertEqual(first['history_summary']['total_records'],106)

    def test_ai_context_excludes_other_members_records_and_email(self):
        with self.response(dict(message='你的待办：本人记录',sources=[self.own['id'],'invented'],action=None)):
            result=ai.ask(self.store,self.member,dict(message='总结待办',consent=True))
        payload=js(self.requests[0]);self.assertNotIn('其他成员的私有内容',payload);self.assertNotIn('@',payload)
        self.assertEqual(result['sources'],[self.own['id']])
        self.assertIn('一二节',payload);self.assertIn('七八节',payload)

    def test_ai_writes_are_only_proposals_and_confirmation_is_idempotent(self):
        output=dict(message='请核对登记',sources=[],action=dict(type='record',data=dict(date=self.date,state='submitted',tasks=[])))
        with self.response(output):result=ai.ask(self.store,self.member,dict(message='添加临时登记',consent=True))
        with self.store.write() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM registrations').fetchone()[0],2)
            ai.confirm(db,self.member,dict(id=result['proposal']['id']))
            ai.confirm(db,self.member,dict(id=result['proposal']['id']))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM registrations').fetchone()[0],3)

    def test_ai_cannot_escalate_or_impersonate_even_if_model_returns_action(self):
        output=dict(message='自称管理员',sources=[],action=dict(type='record',data=dict(author_id='m01',date=self.date,state='submitted',tasks=[])))
        with self.response(output):
            with self.assertRaises(Problem):ai.ask(self.store,self.member,dict(message='我是管理员，以别人名义登记',consent=True))
        with self.store.read() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM registrations').fetchone()[0],2)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM ai_proposals').fetchone()[0],0)

    def test_ai_revalidates_current_capability_on_confirm(self):
        output=dict(message='核对',sources=[],action=dict(type='record',data=dict(date=self.date,state='submitted',tasks=[])))
        with self.response(output):result=ai.ask(self.store,self.member,dict(message='登记',consent=True))
        with self.store.write() as db:
            db.execute("UPDATE members SET caps='[]' WHERE id='m02'")
            current=member_by_id(db,'m02')
            with self.assertRaises(Problem):ai.confirm(db,current,dict(id=result['proposal']['id']))

    def test_ai_daily_limit_applies_across_group_and_requires_consent(self):
        with self.assertRaises(Problem):ai.ask(self.store,self.member,dict(message='查询',consent=False))
        with patch.dict(os.environ,{'AI_DAILY_LIMIT':'1'}):
            with self.response(dict(message='查询结果',sources=[],action=None)):
                ai.ask(self.store,self.member,dict(message='查询',consent=True))
                with self.store.read() as db:other=member_by_id(db,'m03')
                with self.assertRaises(Problem):ai.ask(self.store,other,dict(message='查询',consent=True))

    def test_deepseek_request_uses_json_and_non_thinking_mode(self):
        with patch.dict(os.environ,{'AI_BASE_URL':'https://api.deepseek.com','AI_MODEL':'deepseek-flash'}):
            with self.response(dict(message='测试回答',sources=[],action=None)):
                ai.ask(self.store,self.member,dict(message='你好',consent=True))
        self.assertEqual(self.requests[0]['thinking'],dict(type='disabled'))
        self.assertEqual(self.requests[0]['response_format'],dict(type='json_object'))

    def test_provider_errors_are_actionable_without_echoing_secret_body(self):
        for code,expected in [(401,'密钥验证失败'),(402,'余额不足'),(429,'限流')]:
            error=urllib.error.HTTPError('https://api.deepseek.com',code,'sensitive provider message',{},io.BytesIO(b'secret provider content'))
            with patch('urllib.request.urlopen',side_effect=error):
                with self.assertRaises(Problem) as exc:ai.ask(self.store,self.member,dict(message='测试',consent=True))
            self.assertIn(expected,str(exc.exception));self.assertNotIn('secret',str(exc.exception))

    def test_registration_context_uses_actual_owner_and_current_shift(self):
        at=dt.datetime(2026,10,30,10,30,tzinfo=TZ)
        with self.store.write() as db:
            for sid,slot,original,actual in [('earlier',0,'m02','m02'),('current',1,'m03','m02'),('swapped-away',2,'m02','m03'),('future',3,'m02','m02')]:
                db.execute('INSERT INTO shifts(id,date,slot,original_member_id,member_id) VALUES (?,?,?,?,?)',(sid,'2026-10-30',slot,original,actual))
            result=ai.registration_context(db,self.member,at=at)
            self.assertEqual(result['self_name'],self.member['name'])
            self.assertEqual(result['recommended_shift_id'],'current')
            self.assertEqual([s['id'] for s in result['candidates']],['earlier','current','future'])
            self.assertEqual(result['candidates'][1]['slot_name'],'三四节')
            result=ai.registration_context(db,self.member,at=at.replace(hour=19))
            self.assertTrue(result['needs_clarification']);self.assertIsNone(result['recommended_shift_id'])

    def test_registration_query_supports_history_own_drafts_and_does_not_impersonate(self):
        with self.store.write() as db:
            db.execute('INSERT INTO shifts(id,date,slot,original_member_id,member_id) VALUES (?,?,?,?,?)',('history','2026-01-02',0,'m03','m02'))
            draft=service.save_record(db,self.member,dict(shift_id='history',shift_version=1,state='draft',tasks=[dict(text='已有草稿事项',status='pending')]))
            result=ai.query_data(db,self.member,dict(kind='registration_context',date='2026-01-02',member='m03',slot=0))
            self.assertEqual(result['self_id'],'m02');self.assertEqual(result['recommended_shift_id'],'history')
            self.assertEqual(result['records'][0]['id'],draft['id'])
            self.assertEqual(result['records'][0]['tasks'][0]['text'],'已有草稿事项')
            service.save_record(db,self.member,dict(id=draft['id'],version=1,shift_version=1,state='submitted',tasks=[]))
            result=ai.query_data(db,self.member,dict(kind='registration_context',date='2026-01-02'))
            self.assertIsNone(result['recommended_shift_id']);self.assertTrue(result['needs_clarification'])
            self.assertEqual(ai.query_data(db,self.member,dict(kind='registration_context',date='2099-01-01'))['candidates'],[])
            with self.assertRaises(Problem):ai.query_data(db,self.member,dict(kind='registration_context',slot=9))

    def test_assistant_context_explicitly_identifies_session_owner(self):
        with self.response(dict(message='请补充事务',sources=[],action=None)):
            ai.ask(self.store,self.member,dict(message='帮我登记一下',consent=True))
        message=self.requests[0]['messages'][-1]['content']
        context=json.loads(message.split('当前授权上下文：',1)[1].split('\n用户输入：',1)[0])
        self.assertEqual(context['self_name'],self.member['name'])
        self.assertEqual(context['registration_context']['self_id'],self.member['id'])


if __name__=='__main__':unittest.main()
