import base64
import datetime as dt
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from app import ai, record_import, service
from app.core import CAPS, TZ, Problem, Store, js, member_by_id, row
from app.xlsx import RECORD_HEADERS, read_records, record_tasks, workbook


class RecordImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.store=Store(Path(self.tmp.name)/'db.sqlite3');self.store.init()
        self.date=(dt.datetime.now(TZ).date()-dt.timedelta(days=2)).isoformat()
        with self.store.write() as db:
            db.execute('UPDATE members SET caps=? WHERE id=?',(js(CAPS),'m01'))
            self.admin=member_by_id(db,'m01');self.member=member_by_id(db,'m02')

    def tearDown(self):self.tmp.cleanup()

    def file(self,values):return dict(file=base64.b64encode(workbook(RECORD_HEADERS,values)).decode())
    def sample(self,slot='一二节',text='1. 整理档案\n2. 擦桌子'):
        return [self.member['name'],self.date,slot,text,'是','历史备注','已提交','']

    def test_import_preview_confirm_history_hours_and_repeat(self):
        with self.store.write() as db:
            data=self.file([self.sample(),self.sample('值班时间外','临时取材料'),self.sample('三四节','')])
            preview=record_import.prepare(db,self.admin,data)
            self.assertEqual(preview['errors'],[]);self.assertEqual(preview['new_shifts'],2)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM registrations').fetchone()[0],0)
            record_import.confirm(db,self.admin,dict(id=preview['id']))
            report=service.workload(db,self.admin,dict(member='m02'))
            self.assertEqual(report['member']['regular_hours'],4);self.assertEqual(report['member']['outside_count'],1)
            self.assertTrue(any(r['tasks'] and r['tasks'][1]['text']=='日常卫生' for r in report['regular']))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM record_history WHERE actor_id="m01"').fetchone()[0],3)
            self.assertTrue(record_import.confirm(db,self.admin,dict(id=preview['id']))['already_applied'])
            again=record_import.prepare(db,self.admin,data)
            self.assertEqual(again['counts'],dict(add=0,skip=3))
            # The system's own exported workbook can be read back without duplicates.
            exported=service.export_records(db,self.admin,dict(state='submitted'))
            again=record_import.prepare(db,self.admin,dict(file=base64.b64encode(exported).decode()))
            self.assertEqual(again['errors'],[]);self.assertEqual(again['counts']['add'],0)

    def test_permission_enforced_on_preview_confirm_and_ai(self):
        with self.store.write() as db:
            p=record_import.prepare(db,self.admin,self.file([self.sample()]))
            for fn,arg in [(record_import.prepare,self.file([self.sample()])),(record_import.confirm,dict(id=p['id'])),(service.attendance,{}),(ai.query_data,dict(kind='attendance'))]:
                with self.assertRaises(Problem) as e:fn(db,self.member,arg)
                self.assertEqual(e.exception.status,403)

    def test_conflicting_rows_block_whole_batch_and_stale_preview_rejected(self):
        with self.store.write() as db:
            p=record_import.prepare(db,self.admin,self.file([self.sample(),self.sample(text='不同事项')]))
            self.assertTrue(p['errors'])
            with self.assertRaises(Problem):record_import.confirm(db,self.admin,dict(id=p['id']))
            p=record_import.prepare(db,self.admin,self.file([self.sample()]))
            service.save_record(db,self.member,dict(date=self.date,state='submitted',tasks=[]))
            with self.assertRaises(Problem) as e:record_import.confirm(db,self.admin,dict(id=p['id']))
            self.assertEqual(e.exception.status,409)

    def test_existing_draft_or_other_assignee_not_overwritten(self):
        with self.store.write() as db:
            db.execute('INSERT INTO shifts(id,date,slot,original_member_id,member_id) VALUES (?,?,?,?,?)',('s',self.date,0,'m02','m03'))
            p=record_import.prepare(db,self.admin,self.file([self.sample()]))
            self.assertTrue(p['errors'])
            db.execute('UPDATE shifts SET member_id="m02" WHERE id="s"')
            service.save_record(db,self.member,dict(shift_id='s',shift_version=1,state='draft',tasks=[]))
            p=record_import.prepare(db,self.admin,self.file([self.sample()]))
            self.assertTrue(p['errors'])

    def test_unknown_member_future_date_and_bad_sheet_rejected(self):
        unknown=self.sample();unknown[0]='不存在的人'
        future=self.sample();future[1]='2099-01-01'
        with self.store.write() as db:
            p=record_import.prepare(db,self.admin,self.file([unknown,future]))
            self.assertEqual(len(p['errors']),2)
        with self.assertRaises(Problem):read_records(workbook(['错误表头'],[['内容']]))

    def test_aliases_excel_dates_and_formula_rejected(self):
        serial=(dt.date.fromisoformat(self.date)-dt.date(1899,12,30)).days
        raw=workbook(['登记人','日期','节次','工作内容'],[[self.member['name'],serial,'七八节','1. 事项']])
        parsed,errors=read_records(raw);self.assertEqual(errors,[]);self.assertEqual(parsed[0]['date'],self.date)
        out=io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(raw)) as source,zipfile.ZipFile(out,'w') as target:
            for name in source.namelist():
                data=source.read(name)
                if name=='xl/worksheets/sheet1.xml':data=data.replace(b'<c r="D2"',b'<c r="D2"',1).replace(b'<is><t xml:space="preserve">1. ',b'<f>1+1</f><is><t xml:space="preserve">1. ')
                target.writestr(name,data)
        parsed,errors=read_records(out.getvalue());self.assertTrue(errors);self.assertEqual(parsed,[])

    def test_attendance_distinguishes_drafts_missing_and_no_work_with_actual_owner(self):
        tomorrow=(dt.datetime.now(TZ).date()+dt.timedelta(days=1)).isoformat()
        with self.store.write() as db:
            for n,date,slot,active in [('no',self.date,0,1),('draft',self.date,1,1),('missing',self.date,2,1),('canceled',self.date,3,0),('future',tomorrow,0,1)]:
                db.execute('INSERT INTO shifts(id,date,slot,original_member_id,member_id,active) VALUES (?,?,?,?,?,?)',(n,date,slot,'m03','m02',active))
            service.save_record(db,self.member,dict(shift_id='no',shift_version=1,state='submitted',tasks=[]))
            service.save_record(db,self.member,dict(shift_id='draft',shift_version=1,state='draft',tasks=[]))
            result=ai.query_data(db,self.admin,dict(kind='attendance',member='m02'))
            self.assertEqual(result['counts'],dict(no_work=1,draft=1,missing=1,worked=0))
            self.assertEqual(result['match_count'],3)
            self.assertEqual([i['slot_name'] for i in result['items']],['一二节','三四节','五六节'])
            self.assertEqual(service.attendance(db,self.admin,dict(member='m03'))['match_count'],0)
            self.assertEqual(service.attendance(db,self.admin,dict(status='unsubmitted'))['match_count'],2)
            self.assertEqual(service.attendance(db,self.admin,dict(status='no_work'))['items'][0]['hours'],2)
            self.assertEqual(service.attendance(db,self.admin,{'from':tomorrow})['match_count'],0)

    def test_collection_form_preserves_handover_source_and_original_submission(self):
        headers=['提交时间（自动）','姓名（必填）','事务日期（必填）','工作时段（必填）','事务内容（必填）','是否完成（必填）','其他内容补充','事务内容（已删除）','事务内容（已删除）']
        stamp='2026年1月2日 12:03'
        values=[stamp,self.member['name'],'2026年1月2日','8:00 - 10:00','（1）日常卫生（2）整理001-105班','转交李宥达剩余材料','原备注','旧内容1','旧内容2']
        raw=workbook(headers,[values,values])
        parsed,errors=read_records(raw)
        self.assertEqual(errors,[]);self.assertEqual(parsed[0]['slot'],0)
        self.assertEqual(parsed[0]['tasks'],['日常卫生','整理001-105班'])
        self.assertEqual(parsed[0]['status'],'pending')
        self.assertIn('转交李宥达',parsed[0]['notes'])
        self.assertEqual(parsed[0]['original']['I']['value'],'旧内容2')
        with self.store.write() as db:
            p=record_import.prepare(db,self.admin,dict(file=base64.b64encode(raw).decode()))
            self.assertEqual(p['errors'],[]);self.assertEqual(p['counts'],dict(add=1,skip=1))
            record_import.confirm(db,self.admin,dict(id=p['id']))
            r=row(db,'SELECT * FROM registrations')
            self.assertEqual(r['submitted_at'],'2026-01-02T12:03:00+08:00')
        values[3]='值班时间外（针对你个人的值班时间）';values[4]='无';values[5]='是'
        parsed,errors=read_records(workbook(headers,[values]))
        self.assertEqual(errors,[]);self.assertIsNone(parsed[0]['slot']);self.assertEqual(parsed[0]['tasks'],[])

    def test_legacy_numbering_does_not_split_class_ids_or_dates(self):
        for content in ['1 看办公室\n2 打扫卫生','1.看办公室 2.打扫卫生','1、看办公室 2，打扫卫生']:
            self.assertEqual(record_tasks(content),['看办公室','打扫卫生'])
        self.assertEqual(record_tasks('1.整理105-602班\n2.填写2026年10月报告'),['整理105-602班','填写2026年10月报告'])
        self.assertEqual(record_tasks('106教室借用表制作'),['106教室借用表制作'])


if __name__=='__main__':unittest.main()
