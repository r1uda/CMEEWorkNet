"""Bounded OOXML reader/writer for the agreed schedule format. No formulas execute."""
import datetime as dt
import io
import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

from .core import Problem, SLOTS, TZ, require, valid_date

NS = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'


def read_schedule(data):
    require(len(data) <= 8 * 1024 * 1024, 'Excel 文件不能超过 8 MB')
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        require(len(z.infolist()) < 1000 and sum(f.file_size for f in z.infolist()) < 40 * 1024 * 1024, 'Excel 解压后过大')
        workbook = ET.fromstring(z.read('xl/workbook.xml'))
        sheet = next((s for s in workbook.findall('s:sheets/s:sheet', NS) if s.get('name') == '签到表'), None)
        require(sheet is not None, '缺少名为“签到表”的工作表')
        rels = ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))
        target = next((r.get('Target') for r in rels if r.get('Id') == sheet.get(f'{{{REL}}}id')), '')
        path = posixpath.normpath(target.lstrip('/') if target.startswith('/') else 'xl/' + target)
        require(path.startswith('xl/'), '工作表路径无效')
        strings = []
        if 'xl/sharedStrings.xml' in z.namelist():
            strings = [''.join(t.text or '' for t in s.findall('s:t',NS)+s.findall('s:r/s:t',NS)) for s in ET.fromstring(z.read('xl/sharedStrings.xml'))]
        root = ET.fromstring(z.read(path))
        values = {}
        for cell in root.findall('.//s:sheetData/s:row/s:c', NS):
            reference = cell.get('r', '')
            if cell.find('s:f', NS) is not None:
                value = {'formula': True}
            elif cell.get('t') == 'inlineStr':
                value = ''.join(t.text or '' for t in cell.findall('.//s:t', NS))
            else:
                v = cell.find('s:v', NS)
                value = v.text if v is not None else ''
                if cell.get('t') == 's' and value:
                    value = strings[int(value)]
            values[reference] = value or ''
        for col, expected in [('A', '节次'), ('B', '一二节'), ('D', '三四节'), ('F', '五六节'), ('H', '七八节')]:
            header = values.get(f'{col}2','')
            require(isinstance(header,str) and header.strip() == expected, f'{col}2 应为“{expected}”，请检查表格格式')
        date1904 = workbook.find('s:workbookPr', NS)
        epoch = dt.datetime(1904, 1, 1) if date1904 is not None and date1904.get('date1904') in ('1', 'true') else dt.datetime(1899, 12, 30)
        indexes = sorted({int(re.search(r'\d+$', k).group()) for k in values if re.fullmatch(r'[A-Z]+\d+', k) and int(re.search(r'\d+$', k).group()) >= 3})
        require(len(indexes) <= 10000, '文件超过 10000 行，请分批导入')
        result, errors = [], []
        for index in indexes:
            relevant = [values.get(f'{col}{index}', '') for col in ['A', 'B', 'D', 'F', 'H']]
            if not any(relevant):
                continue
            raw_date = relevant[0]
            try:
                require(not isinstance(raw_date, dict), '日期不能使用公式')
                if re.fullmatch(r'\d+(\.\d+)?', raw_date):
                    value = float(raw_date)
                    require(value == int(value), '日期不能含有时间')
                    date = valid_date((epoch + dt.timedelta(days=value)).date().isoformat())
                else:
                    date = valid_date(raw_date.strip().replace('/', '-'))
            except (Problem, ValueError, OverflowError) as exc:
                errors.append(f'A{index}：{exc}')
                continue
            for slot, col in enumerate(['B', 'D', 'F', 'H']):
                name = values.get(f'{col}{index}', '')
                if isinstance(name, dict):
                    errors.append(f'{col}{index}：值班人不能使用公式')
                elif not name.strip():
                    errors.append(f'{col}{index}：值班人为空，无人值班请明确填写“空”')
                else:
                    result.append(dict(date=date, slot=slot, name=name.strip(), cell=f'{col}{index}'))
        require(result or errors, '工作表中没有排班数据')
        return result, errors
    except Problem:
        raise
    except (zipfile.BadZipFile, KeyError, ET.ParseError, ValueError, IndexError, TypeError):
        raise Problem('无法读取 XLSX，请上传有效的 Excel 工作簿')


def workbook(headers, records, title='事务记录'):
    def col(index):
        output = ''
        while index:
            index, rem = divmod(index - 1, 26)
            output = chr(65 + rem) + output
        return output

    def xml_text(value):
        value = '' if value is None else str(value)
        value = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', value)[:32767]
        return escape(value)

    lines = []
    for n, values in enumerate([headers] + list(records), 1):
        cells = ''.join(f'<c r="{col(i)}{n}" t="inlineStr" s="{1 if n == 1 else 0}"><is><t xml:space="preserve">{xml_text(v)}</t></is></c>' for i, v in enumerate(values, 1))
        lines.append(f'<row r="{n}">{cells}</row>')
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>')
        z.writestr('_rels/.rels', f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="{REL}/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr('xl/workbook.xml', f'<workbook xmlns="{NS["s"]}" xmlns:r="{REL}"><sheets><sheet name="{xml_text(title)}" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels', f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="{REL}/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="{REL}/styles" Target="styles.xml"/></Relationships>')
        z.writestr('xl/styles.xml', f'<styleSheet xmlns="{NS["s"]}"><fonts count="2"><font><sz val="11"/><name val="Microsoft YaHei"/></font><font><b/><sz val="11"/><name val="Microsoft YaHei"/></font></fonts><fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf/></cellStyleXfs><cellXfs count="2"><xf fontId="0" fillId="0" borderId="0" xfId="0"><alignment wrapText="1" vertical="top"/></xf><xf fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')
        z.writestr('xl/worksheets/sheet1.xml', f'<worksheet xmlns="{NS["s"]}"><sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews><cols><col min="1" max="3" width="18" customWidth="1"/><col min="4" max="4" width="55" customWidth="1"/><col min="5" max="8" width="24" customWidth="1"/></cols><sheetData>{"".join(lines)}</sheetData><autoFilter ref="A1:{col(len(headers))}{len(lines)}"/></worksheet>')
    return stream.getvalue()


def schedule_template():
    # This is a blank format, never production or demo schedule data.
    return workbook(['值班安排（请填写日期、姓名或“空”）'], [['节次', '一二节', '签到', '三四节', '签到', '五六节', '签到', '七八节', '签到']], '签到表')


RECORD_HEADERS = ['姓名','事务日期','工作时段','事务内容','是否完成','其他内容补充','登记状态','首次提交时间']


def record_header(value):
    if not value or re.search(r'[（(]已删除[）)]$',value):return ''
    return re.sub(r'[（(](必填|自动)[）)]$','',value)


def record_tasks(content):
    if content in ('','无','无事务','无具体事务','没工作'):return []
    # Only recognize a sequential numbered list; years, class IDs and quantities stay intact.
    marker=re.compile(r'(?<!\d)(?:[（(](\d{1,2})[）)]|(\d{1,2})(?:[.、．）)，,](?!\d)|[ \t]+))\s*')
    matches=list(marker.finditer(content))
    if matches and not content[:matches[0].start()].strip() and [int(m.group(1) or m.group(2)) for m in matches]==list(range(1,len(matches)+1)):
        return [content[m.end():matches[i+1].start() if i+1<len(matches) else len(content)].strip() for i,m in enumerate(matches) if content[m.end():matches[i+1].start() if i+1<len(matches) else len(content)].strip()]
    return [t.strip() for t in content.splitlines() if t.strip()]


def read_records(data):
    """Read record tables, not the separate matrix-style schedule template."""
    require(len(data)<=8*1024*1024,'Excel 文件不能超过 8 MB')
    aliases = {'登记人':'姓名','值班人':'姓名','日期':'事务日期','值班日期':'事务日期',
               '节次':'工作时段','时段':'工作时段','工作内容':'事务内容','工作事项':'事务内容',
               '备注':'其他内容补充','补充说明':'其他内容补充','提交时间':'首次提交时间'}
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            require(len(z.infolist())<1000 and sum(f.file_size for f in z.infolist())<40*1024*1024,'Excel 解压后过大')
            book=ET.fromstring(z.read('xl/workbook.xml'))
            rels={r.get('Id'):r.get('Target','') for r in ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))}
            strings=[]
            if 'xl/sharedStrings.xml' in z.namelist():
                strings=[''.join(s.itertext()) for s in ET.fromstring(z.read('xl/sharedStrings.xml'))]
            prop=book.find('s:workbookPr',NS)
            epoch=dt.date(1904,1,1) if prop is not None and prop.get('date1904') in ('1','true') else dt.date(1899,12,30)
            records,errors=[],[]
            found=False
            for sheet in book.findall('s:sheets/s:sheet',NS):
                target=rels.get(sheet.get(f'{{{REL}}}id'),'')
                path=posixpath.normpath(target.lstrip('/') if target.startswith('/') else 'xl/'+target)
                require(path.startswith('xl/'),'工作表路径无效')
                root=ET.fromstring(z.read(path));headers=None;original_headers={};collection_form=False
                for index,r in enumerate(root.findall('s:sheetData/s:row',NS),1):
                    require(index<=10002,'文件行数过多，请分批导入')
                    cells={}
                    for c in r.findall('s:c',NS):
                        col=re.sub(r'\d','',c.get('r',''))
                        v=c.find('s:v',NS)
                        value=v.text or '' if v is not None else ''
                        if c.get('t')=='inlineStr':value=''.join(t.text or '' for t in c.findall('.//s:t',NS))
                        elif c.get('t')=='s' and value:value=strings[int(value)]
                        cells[col]=None if c.find('s:f',NS) is not None else value.strip()
                    if headers is None:
                        mapped={col:aliases.get(record_header(v),record_header(v)) for col,v in cells.items() if record_header(v)}
                        mapped={col:v for col,v in mapped.items() if v in RECORD_HEADERS}
                        if set(RECORD_HEADERS[:4]).issubset(mapped.values()):
                            require(len(mapped.values())==len(set(mapped.values())),'表头重复，请保留唯一的列名')
                            headers=mapped;found=True;original_headers=cells.copy()
                            collection_form='提交时间（自动）' in cells.values() and '是否完成（必填）' in cells.values()
                        elif index>=20:break
                        continue
                    if not any(v is None or bool(v) for v in cells.values()):continue
                    label=f"{sheet.get('name')}!第{r.get('r',str(index))}行"
                    try:
                        require(all(cells.get(c,'') is not None for c in headers),'记录字段不能使用公式，请先粘贴为值')
                        item={name:cells.get(col,'') for col,name in headers.items()}
                        raw=item['事务日期']
                        if re.fullmatch(r'\d+(\.\d+)?',raw):
                            serial=float(raw);require(serial.is_integer(),'日期不能包含时间')
                            date=valid_date((epoch+dt.timedelta(days=serial)).isoformat())
                        else:
                            raw=raw.replace('/','-').replace('年','-').replace('月','-').rstrip('日')
                            parts=raw.split('-')
                            require(len(parts)==3,'日期须包含年、月、日')
                            date=valid_date(dt.date(*map(int,parts)).isoformat())
                        require(date<=dt.datetime.now(TZ).date().isoformat(),'历史记录不能填写未来日期')
                        names=[s['name'] for s in SLOTS]
                        slot=item['工作时段']
                        ranges={f"{s['start']}-{s['end']}":s['name'] for s in SLOTS}
                        compact=re.sub(r'\s+','',slot).replace('：',':').replace('—','-').replace('–','-')
                        if compact.startswith('8:'):compact='0'+compact
                        slot=ranges.get(compact,slot)
                        if slot=='值班时间外（针对你个人的值班时间）':slot='值班时间外'
                        require(slot in names+['值班时间外','值班外','工作时间之外'],'工作时段须为一二节、三四节、五六节、七八节或值班时间外')
                        require(item.get('登记状态','') in ('','已提交'),'仅导入已提交记录，草稿请另行处理')
                        complete=item.get('是否完成','')
                        known_complete=complete in ('','是','否','已完成','未完成','不适用')
                        require(known_complete or collection_form,'是否完成须为是、否或不适用')
                        content=item['事务内容']
                        tasks=record_tasks(content)
                        require(len(tasks)<=50 and all(len(t)<=4000 for t in tasks),'事务条数或长度超限')
                        require(not tasks or complete!='不适用','有事务时不能填写不适用')
                        notes=item.get('其他内容补充','')
                        if not known_complete:notes+='\n原表完成/交接说明：'+complete
                        notes=notes.strip();require(len(notes)<=10000,'补充说明过长')
                        submitted_at=None
                        if collection_form and item.get('首次提交时间'):
                            submitted=dt.datetime.strptime(item['首次提交时间'],'%Y年%m月%d日 %H:%M').replace(tzinfo=TZ)
                            require(submitted<=dt.datetime.now(TZ),'提交时间不能在未来')
                            submitted_at=submitted.isoformat(timespec='seconds')
                        original={col:{'header':header,'value':cells.get(col,'')} for col,header in original_headers.items() if header}
                        records.append(dict(name=item['姓名'],date=date,slot=names.index(slot) if slot in names else None,tasks=tasks,status='done' if complete in ('是','已完成') else 'pending',notes=notes,source=label,original=original,submitted_at=submitted_at))
                        require(len(records)<=2000,'每批最多导入 2000 条记录')
                    except (Problem,ValueError,OverflowError) as exc:errors.append(f'{label}：{exc}')
            require(found,'未找到事务表表头：姓名、事务日期、工作时段、事务内容。请使用导入模板；排班矩阵请从管理工作台导入。')
            require(records or errors,'表内没有记录')
            return records,errors
    except Problem:raise
    except (zipfile.BadZipFile,KeyError,ET.ParseError,ValueError,IndexError,TypeError):
        raise Problem('无法读取 XLSX，请将旧版 XLS 文件另存为 XLSX 后上传')
