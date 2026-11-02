"""Administrator-only historical imports; preview first, never overwrite records."""
import base64
import datetime as dt
import json

from .core import TZ, Problem, audit, digest, js, now, permit, record_view, require, row, rows, uid
from .xlsx import read_records


def baseline(db):
    return digest(js([rows(db,'SELECT * FROM shifts ORDER BY id'),
                      rows(db,'SELECT id,version FROM registrations ORDER BY id'),
                      rows(db,'SELECT id,name,active FROM members ORDER BY id')]))


def prepare(db,member,body):
    permit(member,'can_manage_members')
    try:raw=base64.b64decode(body.get('file',''),validate=True)
    except (ValueError,TypeError):raise Problem('文件编码无效')
    items,errors=read_records(raw)
    from .service import normalize_task
    people={m['name']:m['id'] for m in rows(db,'SELECT id,name FROM members WHERE active=1')}
    changes=[];seen=set();shifts=set()
    for item in items:
        try:
            require(item['name'] in people,'姓名未匹配到成员，请核对姓名')
            item['author_id']=people[item['name']]
            item['tasks']=[normalize_task(t) for t in item['tasks']]
            key=digest(js({k:item[k] for k in ('author_id','date','slot','tasks','status','notes')}))
            item['fingerprint']=key;item['action']='add';item['shift_id']=None
            if key in seen or row(db,'SELECT fingerprint FROM record_import_sources WHERE fingerprint=?',(key,)):
                item['action']='skip'
            elif item['slot'] is not None:
                pair=(item['date'],item['slot'])
                require(pair not in shifts,'同一日期和班次出现不同记录，请合并为一行')
                shifts.add(pair)
                shift=row(db,'SELECT * FROM shifts WHERE date=? AND slot=?',pair)
                if shift:
                    require(shift['active'] and shift['member_id']==item['author_id'],'与已有实际值班人或已取消排班冲突')
                    item['shift_id']=shift['id']
                    record=row(db,'SELECT * FROM registrations WHERE shift_id=?',(shift['id'],))
                    if record:
                        saved=record_view(db,record)
                        same=record['state']=='submitted' and record['notes']==item['notes'] and [t['text'] for t in saved['tasks']]==item['tasks'] and all(t['status']==item['status'] for t in saved['tasks'])
                        require(same,'该班次已有不同登记或草稿；请从管理员修改入口处理')
                        item['action']='skip'
            else:
                # Reimporting an export must not duplicate outside-work records either.
                for record in rows(db,"SELECT * FROM registrations WHERE author_id=? AND date=? AND shift_id IS NULL AND state='submitted'",(item['author_id'],item['date'])):
                    saved=record_view(db,record)
                    if record['notes']==item['notes'] and [t['text'] for t in saved['tasks']]==item['tasks'] and all(t['status']==item['status'] for t in saved['tasks']):item['action']='skip';break
            seen.add(key);changes.append(item)
        except Problem as exc:errors.append(f"{item['source']}：{exc}")
    payload=dict(changes=changes,errors=errors,counts={a:sum(i['action']==a for i in changes) for a in ('add','skip')},new_shifts=sum(i['action']=='add' and i['slot'] is not None and not i['shift_id'] for i in changes))
    preview_id=uid()
    db.execute('INSERT INTO record_import_previews VALUES (?,?,?,?,?,NULL)',(preview_id,member['id'],js(payload),baseline(db),now()))
    return dict(id=preview_id,**payload)


def confirm(db,member,body):
    permit(member,'can_manage_members')
    preview=row(db,'SELECT * FROM record_import_previews WHERE id=? AND actor_id=?',(body.get('id'),member['id']))
    require(preview,'导入预览不存在',404)
    if preview['applied_at']:return dict(already_applied=True)
    require(dt.datetime.fromisoformat(preview['created_at'])+dt.timedelta(hours=1)>dt.datetime.now(TZ),'预览已过期，请重新上传',409)
    require(preview['baseline']==baseline(db),'记录、成员或排班已改变，请重新预览',409)
    payload=json.loads(preview['payload']);require(not payload['errors'],'请先解决所有错误行')
    from .service import invalidate_mail
    stamp=now()
    for item in payload['changes']:
        if item['action']=='skip':continue
        shift_id=item['shift_id']
        if item['slot'] is not None and not shift_id:
            shift_id=uid()
            db.execute('INSERT INTO shifts(id,date,slot,original_member_id,member_id) VALUES (?,?,?,?,?)',(shift_id,item['date'],item['slot'],item['author_id'],item['author_id']))
        record_id=uid()
        db.execute('INSERT INTO registrations VALUES (?,?,?,?,?,?,?,?,?,?,?)',(record_id,shift_id,item['author_id'],item['date'],item['slot'],'submitted',item['notes'],1,stamp,stamp,item.get('submitted_at') or stamp))
        for position,text in enumerate(item['tasks']):
            task_id=uid()
            db.execute('INSERT INTO tasks VALUES (?,?,?,?,1,?)',(task_id,record_id,text,item['status'],item['author_id']))
            db.execute('INSERT INTO task_positions VALUES (?,?)',(task_id,position))
        saved=record_view(db,row(db,'SELECT * FROM registrations WHERE id=?',(record_id,)))
        db.execute('INSERT INTO record_history VALUES (?,?,?,?,?)',(uid(),record_id,member['id'],js(saved),stamp))
        db.execute('INSERT INTO record_import_sources VALUES (?,?,?,?)',(item['fingerprint'],record_id,preview['id'],item['source']))
        if shift_id:invalidate_mail(db,shift_id)
    db.execute('UPDATE record_import_previews SET applied_at=? WHERE id=?',(stamp,preview['id']))
    audit(db,member['id'],'records.import',dict(preview_id=preview['id'],counts=payload['counts'],new_shifts=payload['new_shifts']))
    return dict(counts=payload['counts'])
