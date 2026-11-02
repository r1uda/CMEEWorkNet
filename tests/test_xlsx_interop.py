"""Optional independent OOXML interoperability checks using bundled openpyxl."""
import datetime as dt
import io
import unittest

from app.core import NAMES
from app.xlsx import read_schedule, workbook

try:
    import openpyxl
except ImportError:
    openpyxl=None


@unittest.skipUnless(openpyxl,'可选互操作检查需要 openpyxl，应用本身不依赖它')
class ExcelInteropTests(unittest.TestCase):
    def test_independent_excel_reader_opens_export_and_treats_formulas_as_text(self):
        data=workbook(['姓名','日期','时段','事务','完成','补充'],[['张梓涵','2026-10-30','一二节','=1+1','是','+SUM(A1:A2)']])
        sheet=openpyxl.load_workbook(io.BytesIO(data)).active
        self.assertEqual(sheet['D2'].value,'=1+1');self.assertEqual(sheet['D2'].data_type,'s')
        self.assertEqual(sheet['F2'].data_type,'s');self.assertEqual(sheet.max_row,2)

    def test_synthetic_22_dates_79_members_9_empty_with_excel_dates_and_merged_title(self):
        # Reconstructs the DOCUMENTED SHAPE only. This is NOT the missing original workbook.
        book=openpyxl.Workbook();sheet=book.active;sheet.title='签到表';sheet.merge_cells('B1:I1');sheet['B1']='合成格式验收数据'
        for col,value in enumerate(['节次','一二节','签到','三四节','签到','五六节','签到','七八节','签到'],1):sheet.cell(2,col,value)
        dates=[];date=dt.date(2026,10,29)
        while date<=dt.date(2026,11,27):
            if date.weekday()<5:dates.append(date)
            date+=dt.timedelta(days=1)
        self.assertEqual(len(dates),22)
        index=0
        for n,date in enumerate(dates,3):
            sheet.cell(n,1,date)
            for col in [2,4,6,8]:
                sheet.cell(n,col,'空' if index<9 else NAMES[index%10]);index+=1
        output=io.BytesIO();book.save(output)
        parsed,errors=read_schedule(output.getvalue())
        self.assertEqual(errors,[]);self.assertEqual(len(parsed),88)
        self.assertEqual(sum(r['name']=='空' for r in parsed),9)
        self.assertEqual(sum(r['name']!='空' for r in parsed),79)
        self.assertEqual(parsed[0]['date'],'2026-10-29')


if __name__=='__main__':unittest.main()
