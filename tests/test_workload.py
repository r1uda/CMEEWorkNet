import datetime as dt
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import ai, service
from app.core import CAPS, TZ, Problem, Store, js, member_by_id, row, uid


class WorkloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.store=Store(Path(self.tmp.name)/'db.sqlite3');self.store.init()
        self.date=dt.datetime.now(TZ).date().isoformat()
        with self.store.write() as db:
            db.execute('UPDATE members SET caps=? WHERE id=?',(js(CAPS),'m01'))
            self.admin=member_by_id(db,'m01');self.member=member_by_id(db,'m02')

    def tearDown(self):self.tmp.cleanup()

    def record(self,slot=None,state='submitted',tasks=None):
        with self.store.write() as db:
            shift=uid() if slot is not None else None
            if shift:db.execute('INSERT INTO shifts(id,date,slot,original_member_id,member_id) VALUES (?,?,?,?,?)',(shift,self.date,slot,'m02','m02'))
            return service.save_record(db,self.member,dict(shift_id=shift,shift_version=1,date=self.date,state=state,tasks=tasks or []))

    def test_hours_count_submitted_shifts_including_no_work_not_drafts_or_extra(self):
        self.record(0);self.record(1,tasks=[dict(text='整理档案',status='done')]);self.record(2,state='draft');self.record(tasks=[dict(text='临时取材料',status='done')])
        with self.store.read() as db:
            result=service.workload(db,self.admin,dict(member='m02'))
            self.assertEqual(result['member']['regular_hours'],4)
            self.assertEqual(len(result['regular']),2)
            self.assertEqual(len(result['outside']),1)
            self.assertEqual(result['outside'][0]['tasks'][0]['text'],'临时取材料')
            self.assertEqual(service.workload(db,self.admin,{'from':'2099-01-01'})['summary'][1]['regular_hours'],0)

    def test_stats_and_ai_stats_are_admin_only_even_if_records_shared(self):
        self.record(0)
        with self.store.write() as db:
            db.execute("UPDATE settings SET value='true' WHERE key='share_records'")
            with self.assertRaises(Problem):service.workload(db,self.member)
            with self.assertRaises(Problem):ai.query_data(db,self.member,dict(kind='workload'))

    def test_admin_corrects_submitted_record_preserving_author_ids_and_history(self):
        saved=self.record(0,tasks=[dict(text='原文',status='pending')])
        with self.store.write() as db:
            payload=dict(id=saved['id'],version=1,shift_version=1,state='submitted',tasks=[dict(id=saved['tasks'][0]['id'],text='更正后的事项',status='done')],change_reason='核对原表更正')
            result=service.admin_save_record(db,self.admin,payload)
            self.assertEqual(result['author_id'],'m02')
            self.assertEqual(result['tasks'][0]['assignee_id'],'m02')
            self.assertEqual(result['version'],2)
            snapshots=[json.loads(r[0]) for r in db.execute('SELECT snapshot FROM record_history WHERE registration_id=?',(saved['id'],))]
            self.assertTrue(any(x['tasks'][0]['text']=='原文' for x in snapshots))
            self.assertTrue(any(x['tasks'][0]['text']=='更正后的事项' for x in snapshots))
            self.assertEqual(row(db,"SELECT actor_id FROM audit WHERE action='record.admin_edit'")['actor_id'],'m01')
            self.assertEqual(service.workload(db,self.admin)['summary'][1]['regular_hours'],2)
            with self.assertRaises(Problem):service.admin_save_record(db,self.admin,payload)

    def test_admin_edit_requires_permission_and_submitted_record_reason_optional(self):
        draft=self.record(state='draft');saved=self.record()
        with self.store.write() as db:
            data=dict(id=saved['id'],version=1,state='submitted',date=self.date,tasks=[],change_reason='更正')
            with self.assertRaises(Problem):service.admin_save_record(db,self.member,data)
            with self.assertRaises(Problem):service.admin_save_record(db,self.admin,{**data,'id':draft['id']})
            self.assertEqual(service.admin_save_record(db,self.admin,{**data,'change_reason':''})['version'],2)
            # Ordinary save route continues to forbid impersonation, including administrators.
            with self.assertRaises(Problem):service.save_record(db,self.admin,data)

    def test_admin_reads_table_without_sharing_but_not_other_drafts(self):
        draft=self.record(state='draft');saved=self.record()
        with self.store.read() as db:
            ids=[r['id'] for r in service.state(db,self.admin)['records']]
            self.assertIn(saved['id'],ids);self.assertNotIn(draft['id'],ids)
            other=member_by_id(db,'m03')
            self.assertEqual(service.state(db,other)['records'],[])

    def test_work_mode_validation_and_precise_synonyms(self):
        saved=self.record(tasks=[dict(text='擦桌子',status='done'),dict(text='给 3 班擦桌子 5 张',status='done')])
        self.assertEqual(saved['tasks'][0]['text'],'日常卫生')
        self.assertEqual(saved['tasks'][1]['text'],'给 3 班擦桌子 5 张')
        with self.store.write() as db:
            for mode,tasks in [('worked',[]),('no_work',[dict(text='内容')])]:
                with self.assertRaises(Problem):service.save_record(db,self.member,dict(date=self.date,state='submitted',work_mode=mode,tasks=tasks))

    def test_inserted_numbered_item_keeps_order_and_original_task_ids(self):
        saved=self.record(tasks=[dict(text='第一项'),dict(text='最后一项')])
        tasks=[dict(id=saved['tasks'][0]['id'],text='第一项'),dict(text='回车插入的第二项'),dict(id=saved['tasks'][1]['id'],text='最后一项')]
        with self.store.write() as db:
            updated=service.save_record(db,self.member,dict(id=saved['id'],version=1,state='submitted',date=self.date,tasks=tasks))
            self.assertEqual([t['text'] for t in updated['tasks']],['第一项','回车插入的第二项','最后一项'])
            self.assertEqual(updated['tasks'][2]['id'],saved['tasks'][1]['id'])

    def test_ai_own_record_queries_do_not_leak_other_records(self):
        saved=self.record(tasks=[dict(text='成员私有内容')])
        with self.store.read() as db:
            other=member_by_id(db,'m03')
            self.assertEqual(ai.query_data(db,other,dict(kind='record',id=saved['id']))['records'],[])
            self.assertEqual(ai.query_data(db,self.admin,dict(kind='record',id=saved['id']))['records'][0]['id'],saved['id'])

    def test_ai_multiturn_queries_statistics_and_export_returns_authorized_download(self):
        self.record(0);payloads=[]
        outputs=[dict(message='查询工时',query=dict(kind='workload',member='m02'),action=None),dict(message='张梓涵累计 2 小时',sources=[],action=dict(type='export',data={'author':'m02'}))]
        def response(req,timeout):
            payloads.append(json.loads(req.data))
            return io.BytesIO(js(dict(choices=[dict(message=dict(content=js(outputs.pop(0))))])).encode())
        with self.store.write() as db:db.execute("UPDATE settings SET value='true' WHERE key='ai_consent'")
        with patch.dict(os.environ,dict(AI_BASE_URL='https://example.invalid/v1',AI_MODEL='test',AI_API_KEY='test',AI_DAILY_LIMIT='30')),patch('urllib.request.urlopen',side_effect=response):
            result=ai.ask(self.store,self.admin,dict(message='这个人工作多久？导出他的登记',consent=True,history=[dict(role='user',content='我说的是张梓涵')]))
        self.assertEqual(len(payloads),2)
        self.assertIn('我说的是张梓涵',js(payloads[0]))
        self.assertIn('regular_hours',js(payloads[1]))
        with self.store.write() as db:
            confirmed=ai.confirm(db,self.admin,dict(id=result['proposal']['id']))
            self.assertTrue(confirmed['result']['download'].startswith('/api/export?'))
            self.assertEqual(confirmed['result']['count'],1)
            self.assertEqual(ai.confirm(db,self.admin,dict(id=result['proposal']['id']))['result']['count'],1)


if __name__=='__main__':unittest.main()
