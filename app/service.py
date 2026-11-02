import base64
import datetime as dt
import json
import re
import sqlite3

from .core import (CAPS, SLOTS, TZ, Problem, audit, can, can_read_record, digest, js,
                   member_by_id, now, permit, record_view, require, row, rows,
                   schedule_baseline, setting, slot_end, text_field, uid, valid_date)
from .xlsx import read_schedule, workbook
from .member_duties import DUTIES, GROUP_ROLES
from . import mailer, swap_mail, record_import, handoff, public_swap, workflow_mail


def owned(db, member, table, object_id, owner='author_id'):
    require(table in ('registrations', 'shifts'), '无效对象')
    obj = row(db, f'SELECT * FROM {table} WHERE id=?', (object_id,))
    require(obj, '记录不存在', 404)
    require(obj[owner] == member['id'], '只能操作自己的记录', 403)
    return obj


def invalidate_mail(db, shift_id):
    db.execute("UPDATE outbox SET status='canceled',error='排班或登记已改变' WHERE kind='reminder' AND shift_id=? AND status IN ('pending','failed')", (shift_id,))
    db.execute('''UPDATE outbox SET status='canceled',error='相关排班或登记已改变'
        WHERE kind='swap_request' AND status IN ('pending','failed') AND id IN
        (SELECT l.outbox_id FROM swap_mail_links l JOIN swaps w ON w.id=l.swap_id
         WHERE w.shift_id=? OR w.target_shift_id=?)''', (shift_id,shift_id))


def state(db, member, filters=None):
    filters = filters or {}
    all_records = bool(setting(db, 'share_records')) or can(member,'can_manage_members')
    all_shifts = bool(setting(db, 'share_schedule')) or can(member, 'can_manage_schedule') or can(member,'can_manage_members')
    me = dict(member)
    me['caps'] = json.loads(me['caps'])
    me['home_order'] = setting(db, 'home_order:' + member['id']) or []
    members = rows(db, 'SELECT id,name' + (',email,caps,active' if can(member, 'can_manage_members') else '') + ' FROM members ORDER BY rowid')
    for person in members:
        person['duties'] = DUTIES.get(person['id'], '')
        person['group_role'] = GROUP_ROLES.get(person['id'], '事务组')
    shifts = rows(db, '''SELECT s.*,m.name AS member_name,o.name AS original_name,r.id AS registration_id,r.state AS registration_state
        FROM shifts s JOIN members m ON m.id=s.member_id JOIN members o ON o.id=s.original_member_id
        LEFT JOIN registrations r ON r.shift_id=s.id WHERE s.active=1''' + ('' if all_shifts else ' AND s.member_id=?') + ' ORDER BY s.date,s.slot', () if all_shifts else (member['id'],))
    conditions, args = ["(r.author_id=? OR (?=1 AND r.state='submitted'))"], [member['id'], int(all_records)]
    if filters.get('from'):
        conditions.append('r.date>=?'); args.append(valid_date(filters['from']))
    if filters.get('to'):
        conditions.append('r.date<=?'); args.append(valid_date(filters['to']))
    if filters.get('author'):
        conditions.append('r.author_id=?'); args.append(filters['author'])
    if filters.get('state') in ('draft', 'submitted'):
        conditions.append('r.state=?'); args.append(filters['state'])
    if filters.get('slot') in ('0','1','2','3','temporary'):
        conditions.append('r.slot IS NULL' if filters['slot'] == 'temporary' else 'r.slot=?')
        if filters['slot'] != 'temporary': args.append(int(filters['slot']))
    result = rows(db, 'SELECT r.*,m.name AS author_name FROM registrations r JOIN members m ON m.id=r.author_id WHERE ' + ' AND '.join(conditions) + ' ORDER BY r.date DESC,COALESCE(r.slot,4) ASC,r.created_at DESC,r.id', args)
    result = [record_view(db, r) for r in result]
    if filters.get('q'):
        term = filters['q'].casefold()
        result = [r for r in result if term in (r['notes'] + ' '.join(t['text'] for t in r['tasks'])).casefold()]
    if filters.get('task_status') in ('pending','done','none'):
        wanted = filters['task_status']
        result = [r for r in result if (not r['tasks'] if wanted == 'none' else (any(t['status'] == 'pending' for t in r['tasks']) if wanted == 'pending' else bool(r['tasks']) and all(t['status'] == 'done' for t in r['tasks'])))]
    messages = rows(db, '''SELECT DISTINCT x.*,m.name AS sender_name,mr.read_at,mr.confirmed_at,n.target_url FROM messages x JOIN members m ON m.id=x.sender_id
        LEFT JOIN notification_links n ON n.message_id=x.id
        LEFT JOIN message_recipients mr ON mr.message_id=x.id AND mr.member_id=?
        WHERE x.sender_id=? OR mr.member_id=? ORDER BY x.created_at DESC,x.rowid DESC''', (member['id'], member['id'], member['id']))
    for message in messages:
        message['recipients'] = rows(db, 'SELECT mr.member_id,m.name,mr.read_at,mr.confirmed_at FROM message_recipients mr JOIN members m ON m.id=mr.member_id WHERE message_id=?', (message['id'],))
    swaps = rows(db, '''SELECT w.*,a.name AS requester_name,b.name AS recipient_name,s.date,s.slot,t.date AS target_date,t.slot AS target_slot,
        o.status AS email_status,o.error AS email_error,(b.email IS NOT NULL) AS recipient_email_configured FROM swaps w
        JOIN members a ON a.id=w.requester_id JOIN members b ON b.id=w.recipient_id JOIN shifts s ON s.id=w.shift_id LEFT JOIN shifts t ON t.id=w.target_shift_id
        LEFT JOIN swap_mail_links l ON l.swap_id=w.id LEFT JOIN outbox o ON o.id=l.outbox_id
        WHERE w.requester_id=? OR w.recipient_id=? ORDER BY w.created_at DESC LIMIT 200''', (member['id'],member['id']))
    for w in swaps:
        result_mail = row(db, """SELECT o.status,o.error FROM outbox o JOIN workflow_mail_links l ON l.outbox_id=o.id
            WHERE l.entity='swap' AND l.entity_id=? ORDER BY o.created_at DESC LIMIT 1""", (w['id'],))
        if result_mail:
            w['result_email_status'], w['result_email_error'] = result_mail['status'], result_mail['error']
    handoffs = handoff.threads(db, member)
    assigned = rows(db, '''SELECT t.*,r.date,r.author_id,m.name AS author_name FROM tasks t JOIN registrations r ON r.id=t.registration_id
        JOIN members m ON m.id=r.author_id WHERE t.active=1 AND r.state='submitted' AND t.assignee_id=? AND r.author_id!=? ORDER BY r.date DESC''', (member['id'],member['id']))
    for task in assigned:
        task['progress'] = rows(db, 'SELECT p.*,m.name AS author_name FROM progress p JOIN members m ON m.id=p.author_id WHERE p.task_id=? ORDER BY p.created_at', (task['id'],))
    config = {k: setting(db,k) for k in ('activation_date','share_records','share_schedule','ai_consent','escalation_recipient','public_swap_mail_mode')}
    mail = rows(db, '''SELECT o.id,o.kind,o.subject,o.status,o.attempts,o.created_at,o.sent_at,o.error,
        COALESCE(s.date,ss.date) AS date,COALESCE(s.slot,ss.slot) AS slot,m.name AS recipient_name FROM outbox o
        LEFT JOIN shifts s ON s.id=o.shift_id JOIN members m ON m.id=o.recipient_id
        LEFT JOIN swap_mail_links l ON l.outbox_id=o.id LEFT JOIN swaps w ON w.id=l.swap_id
        LEFT JOIN shifts ss ON ss.id=w.shift_id ORDER BY o.created_at DESC LIMIT 200''') if can(member,'can_manage_schedule') else []
    return dict(me=me,members=members,shifts=shifts,records=result,messages=messages,swaps=swaps,public_swaps=public_swap.listing(db,member),handoffs=handoffs,assigned=assigned,settings=config,mail=mail,slots=SLOTS,server_time=now(),**handoff.personal_tasks(db,member))


HOME_BLOCKS = ('pending', 'next', 'tasks', 'progress', 'recent', 'notices')


def save_home_order(db, member, body):
    order = body.get('order')
    require(isinstance(order, list) and len(order) == len(HOME_BLOCKS)
            and all(isinstance(item, str) for item in order)
            and set(order) == set(HOME_BLOCKS), '板块顺序无效，请刷新后重试')
    db.execute('INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
               ('home_order:' + member['id'], js(order)))
    return dict(order=order)


def save_record(db, member, body, *, administrative=False):
    permit(member, 'can_manage_members' if administrative else 'can_register')
    require('author_id' not in body, '作者身份由服务器确定', 403)
    record_id = body.get('id')
    previous = (row(db,'SELECT * FROM registrations WHERE id=?',(record_id,)) if administrative else owned(db,member,'registrations',record_id)) if record_id else None
    if administrative:
        require(previous and previous['state']=='submitted','管理员只能修改已提交的登记',403)
        text_field(body.get('change_reason',''),1000)
    owner_id = previous['author_id'] if previous else member['id']
    if previous:
        require(body.get('version') == previous['version'], '记录已改变，请刷新后重试', 409)
        shift_id = previous['shift_id']
    else:
        shift_id = body.get('shift_id') or None
    shift = None
    if shift_id:
        shift = owned(db,dict(id=owner_id),'shifts',shift_id,'member_id')
        require(shift['active'], '班次已取消', 409)
        require(body.get('shift_version') == shift['version'], '排班已改变，请刷新后重试', 409)
        require(shift['date'] <= now()[:10], '尚未到值班日期，请到当天再登记')
        date, slot = shift['date'], shift['slot']
    else:
        date, slot = valid_date(body.get('date')), None
    desired_state = body.get('state')
    require(desired_state in ('draft','submitted'), '登记状态无效')
    if previous and previous['state'] == 'submitted':
        require(desired_state == 'submitted', '已提交记录不能退回草稿')
    notes = text_field(body.get('notes',''))
    tasks = body.get('tasks', [])
    require(isinstance(tasks,list) and len(tasks) <= 50, '一份登记最多 50 件事务')
    mode = body.get('work_mode')
    require(mode in (None,'worked','no_work'),'工作选择无效')
    require(mode!='no_work' or not tasks,'选择没工作时不能同时填写事务')
    require(mode!='worked' or bool(tasks),'选择工作了后，请至少填写一项事务')
    require(not previous or previous['shift_id'] == shift_id, '不能更换登记对应班次')
    old_tasks = {t['id']: t for t in rows(db,'SELECT * FROM tasks WHERE registration_id=?',(record_id,))} if previous else {}
    normalized, seen = [], set()
    for task in tasks:
        require(isinstance(task,dict), '事务格式无效')
        text = normalize_task(text_field(task.get('text',''),4000))
        require(bool(text), '请填写事务内容，或移除空白事务后直接提交')
        status = task.get('status','pending')
        require(status in ('pending','done'), '事务状态无效')
        task_id = task.get('id') or uid()
        require(task_id not in seen, '重复事务标识')
        seen.add(task_id)
        if task.get('id'):
            require(task_id in old_tasks and old_tasks[task_id]['active'], '事务不属于当前记录', 403)
            if old_tasks[task_id]['assignee_id'] != owner_id:
                require(status == old_tasks[task_id]['status'], '已交接事务的状态由当前接手人更新', 409)
        normalized.append((task_id,text,status))
    for task_id in set(old_tasks) - seen:
        require(not row(db,'SELECT id FROM handoffs WHERE task_id=?',(task_id,)), '有交接历史的事务不能移除；可以补充说明')
    stamp, record_id = now(), record_id or uid()
    submitted_at = (previous['submitted_at'] if previous else None) or (stamp if desired_state == 'submitted' else None)
    if previous:
        if administrative:
            db.execute('INSERT INTO record_history VALUES (?,?,?,?,?)',(uid(),record_id,member['id'],js(record_view(db,previous)),stamp))
        db.execute('UPDATE registrations SET date=?,slot=?,state=?,notes=?,version=version+1,updated_at=?,submitted_at=? WHERE id=?', (date,slot,desired_state,notes,stamp,submitted_at,record_id))
    else:
        require(not shift_id or not row(db,'SELECT id FROM registrations WHERE shift_id=?',(shift_id,)), '该班次已有登记，请打开原记录修改',409)
        db.execute('INSERT INTO registrations VALUES (?,?,?,?,?,?,?,?,?,?,?)',(record_id,shift_id,member['id'],date,slot,desired_state,notes,1,stamp,stamp,submitted_at))
    db.execute('UPDATE tasks SET active=0 WHERE registration_id=?',(record_id,))
    for position,(task_id,text,status) in enumerate(normalized):
        if task_id in old_tasks:
            db.execute('UPDATE tasks SET text=?,status=?,active=1 WHERE id=?',(text,status,task_id))
        else:
            db.execute('INSERT INTO tasks VALUES (?,?,?,?,1,?)',(task_id,record_id,text,status,owner_id))
        db.execute('INSERT INTO task_positions VALUES (?,?) ON CONFLICT(task_id) DO UPDATE SET position=excluded.position',(task_id,position))
    saved = record_view(db,row(db,'SELECT * FROM registrations WHERE id=?',(record_id,)))
    db.execute('INSERT INTO record_history VALUES (?,?,?,?,?)',(uid(),record_id,member['id'],js(saved),stamp))
    if shift_id and desired_state == 'submitted':
        invalidate_mail(db,shift_id)
    audit(db,member['id'],'record.admin_edit' if administrative else 'record.save',dict(id=record_id,state=desired_state,version=saved['version'],reason=body.get('change_reason','') if administrative else ''))
    return saved


def normalize_task(text):
    # Exact synonyms only: preserve class names, quantities and other specific details.
    return '日常卫生' if text in ('打扫卫生','擦桌子','丢垃圾','倒垃圾','打扫日常卫生') else text


def admin_save_record(db,member,body):
    return save_record(db,member,body,administrative=True)


def workload(db,member,filters=None):
    permit(member,'can_manage_members')
    filters = filters or {}
    conditions,args = ["r.state='submitted'"],[]
    for key,op in [('from','>='),('to','<=')]:
        if filters.get(key):
            conditions.append('r.date'+op+'?');args.append(valid_date(filters[key]))
    if filters.get('from') and filters.get('to'):
        require(filters['from']<=filters['to'],'起始日期不能晚于结束日期')
    people = rows(db,'SELECT id,name FROM members ORDER BY rowid')
    records = rows(db,'SELECT r.*,m.name AS author_name FROM registrations r JOIN members m ON m.id=r.author_id WHERE '+' AND '.join(conditions)+' ORDER BY r.date DESC,r.slot,r.created_at DESC',args)
    summary = []
    for person in people:
        own = [r for r in records if r['author_id']==person['id']]
        regular = [r for r in own if r['shift_id'] is not None]
        summary.append(dict(**person,regular_count=len(regular),regular_hours=2*len(regular),outside_count=len(own)-len(regular)))
    result = dict(summary=summary,from_date=filters.get('from'),to_date=filters.get('to'),rule='已提交的正常值班每班2小时；无具体事务仍算值班。草稿不计入，值班外只计记录不计工时。')
    if filters.get('member'):
        target = next((p for p in summary if p['id']==filters['member']),None)
        require(target,'成员不存在',404)
        own = [record_view(db,r) for r in records if r['author_id']==target['id']]
        result.update(member=target,regular=[r for r in own if r['shift_id'] is not None],outside=[r for r in own if r['shift_id'] is None])
    return result


def attendance(db,member,filters=None):
    """Ended actual shifts only. Absence of registration never proves absence from work."""
    permit(member,'can_manage_members')
    filters=filters or {}
    conditions=['s.active=1'];args=[]
    for key,op in [('from','>='),('to','<=')]:
        if filters.get(key):conditions.append('s.date'+op+'?');args.append(valid_date(filters[key]))
    if filters.get('from') and filters.get('to'):require(filters['from']<=filters['to'],'起始日期不能晚于结束日期')
    if filters.get('member') or filters.get('author'):
        target=member_by_id(db,filters.get('member') or filters['author'])
        conditions.append('s.member_id=?');args.append(target['id'])
    status=filters.get('status','all')
    require(status in ('all','no_work','missing','draft','unsubmitted'),'登记核对类型无效')
    shifts=rows(db,'''SELECT s.*,m.name AS name,r.id AS record_id,r.state,
        (SELECT COUNT(*) FROM tasks t WHERE t.registration_id=r.id AND t.active=1) AS task_count
        FROM shifts s JOIN members m ON m.id=s.member_id LEFT JOIN registrations r ON r.shift_id=s.id
        WHERE '''+' AND '.join(conditions)+' ORDER BY s.date DESC,s.slot',args)
    items=[];counts=dict(no_work=0,missing=0,draft=0,worked=0)
    for s in shifts:
        if slot_end(s)>dt.datetime.now(TZ):continue
        kind='missing' if not s['record_id'] else 'draft' if s['state']=='draft' else 'no_work' if not s['task_count'] else 'worked'
        counts[kind]+=1
        if status=='all' or status==kind or status=='unsubmitted' and kind in ('missing','draft'):
            items.append(dict(shift_id=s['id'],member_id=s['member_id'],name=s['name'],date=s['date'],slot=s['slot'],slot_name=SLOTS[s['slot']]['name'],status=kind,record_id=s['record_id'],hours=2 if s['state']=='submitted' else 0))
    offset=filters.get('offset',0)
    try:offset=int(offset)
    except (TypeError,ValueError):raise Problem('分页位置无效')
    require(0<=offset<=100000,'分页位置无效')
    return dict(items=items[offset:offset+100],counts=counts,match_count=len(items),next_offset=offset+min(100,max(0,len(items)-offset)),has_more=len(items)>offset+100,
                rule='只核对已结束的有效实际排班。no_work=已登记但无具体事务，仍计2小时；missing=完全未登记；draft=草稿未提交；worked=已提交且有事务。未登记不代表没有实际值班。没有排班或尚未结束的班次不计漏登记。')


def import_preview(db, member, body):
    permit(member,'can_manage_schedule')
    try:
        raw = base64.b64decode(body.get('file',''), validate=True)
    except (ValueError,TypeError):
        raise Problem('文件编码无效')
    parsed, errors = read_schedule(raw)
    members = {m['name']:m['id'] for m in rows(db,'SELECT id,name FROM members WHERE active=1')}
    changes, seen = [], set()
    for item in parsed:
        key = (item['date'],item['slot'])
        if key in seen:
            errors.append(f"{item['cell']}：重复日期及班次")
            continue
        seen.add(key)
        if item['name'] != '空' and item['name'] not in members:
            errors.append(f"{item['cell']}：未知成员“{item['name']}”，请核对姓名")
            continue
        existing = row(db,'SELECT * FROM shifts WHERE date=? AND slot=?',key)
        member_id = members.get(item['name'])
        action = 'unchanged'
        if not existing and member_id:
            action = 'add'
        elif existing:
            if not member_id and existing['active']:
                action = 'remove'
            elif member_id and (not existing['active'] or existing['original_member_id'] != member_id):
                action = 'change'
        protected = existing and (row(db,'SELECT id FROM registrations WHERE shift_id=?',(existing['id'],)) or row(db,"SELECT id FROM swaps WHERE state='accepted' AND (shift_id=? OR target_shift_id=?)",(existing['id'],existing['id'])))
        if action in ('remove','change'):
            if protected:
                errors.append(f"{item['cell']}：班次已有登记或已确认换班，不能通过导入覆盖")
            elif slot_end(existing) <= dt.datetime.now(TZ):
                errors.append(f"{item['cell']}：已有历史班次不能通过普通导入改写，需要人工审计更正")
        changes.append(dict(**item,member_id=member_id,action=action,previous=existing))
    preview_id = uid()
    payload = dict(changes=changes,errors=errors,counts={a:sum(c['action']==a for c in changes) for a in ('add','change','remove','unchanged')},dates=len({i['date'] for i in parsed}),occupied=sum(i['name']!='空' for i in parsed),empty=sum(i['name']=='空' for i in parsed))
    db.execute('INSERT INTO import_previews VALUES (?,?,?,?,?,?,NULL)',(preview_id,member['id'],js(payload),digest(raw.hex()),schedule_baseline(db),now()))
    return dict(id=preview_id,**payload)


def import_confirm(db,member,body):
    permit(member,'can_manage_schedule')
    preview = row(db,'SELECT * FROM import_previews WHERE id=? AND actor_id=?',(body.get('id'),member['id']))
    require(preview,'导入预览不存在',404)
    if preview['applied_at']:
        return dict(already_applied=True)
    require(dt.datetime.fromisoformat(preview['created_at']) + dt.timedelta(hours=1) > dt.datetime.now(TZ),'预览已过期，请重新上传',409)
    require(preview['baseline']==schedule_baseline(db),'排班已改变，请重新预览',409)
    payload = json.loads(preview['payload'])
    require(not payload['errors'],'请先解决预览中的错误')
    for item in payload['changes']:
        existing, action = item['previous'],item['action']
        if action == 'add':
            db.execute('INSERT INTO shifts(id,date,slot,original_member_id,member_id) VALUES (?,?,?,?,?)',(uid(),item['date'],item['slot'],item['member_id'],item['member_id']))
        elif action in ('change','remove'):
            # Registrations can arrive after preview without changing the schedule baseline.
            require(not row(db,'SELECT id FROM registrations WHERE shift_id=?',(existing['id'],)), '预览后有人登记，请重新上传',409)
            require(slot_end(existing)>dt.datetime.now(TZ),'预览中的班次已结束，请重新上传',409)
            if action == 'remove':
                db.execute('UPDATE shifts SET active=0,version=version+1 WHERE id=?',(existing['id'],))
            else:
                db.execute('UPDATE shifts SET original_member_id=?,member_id=?,active=1,version=version+1 WHERE id=?',(item['member_id'],item['member_id'],existing['id']))
            invalidate_mail(db,existing['id'])
            db.execute("UPDATE swaps SET state='stale',decided_at=? WHERE state='pending' AND (shift_id=? OR target_shift_id=?)",(now(),existing['id'],existing['id']))
    db.execute('UPDATE import_previews SET applied_at=? WHERE id=?',(now(),preview['id']))
    audit(db,member['id'],'schedule.import',dict(preview_id=preview['id'],counts=payload['counts']))
    return dict(counts=payload['counts'])


def swap_create(db,member,body,*,public_selection=False):
    permit(member,'can_register')
    shift = owned(db,member,'shifts',body.get('shift_id'),'member_id')
    require(shift['active'] and slot_end(shift)>dt.datetime.now(TZ),'只能申请未结束的有效班次')
    require(body.get('version')==shift['version'],'排班已改变，请刷新',409)
    require(not row(db,'SELECT id FROM registrations WHERE shift_id=?',(shift['id'],)),'班次已有登记或草稿，不能直接换班')
    recipient = member_by_id(db,body.get('recipient_id'))
    require(recipient['id'] != member['id'] and can(recipient,'can_register'),'接手人必须是另一位可登记成员')
    target_id, target_version = body.get('target_shift_id') or None, None
    if target_id:
        target = row(db,'SELECT * FROM shifts WHERE id=?',(target_id,))
        require(target and target['member_id']==recipient['id'] and target['active'],'对调班次无效')
        require(slot_end(target)>dt.datetime.now(TZ),'不能对调已结束班次')
        require(not row(db,'SELECT id FROM registrations WHERE shift_id=?',(target_id,)),'对调班次已有登记')
        require(body.get('target_version')==target['version'],'对调排班已改变',409)
        target_version = target['version']
    public_swap.expire(db)
    require(public_selection or not row(db, "SELECT id FROM public_swaps WHERE shift_id=? AND state='open'", (shift['id'],)), '该班次正在公开求换班，请先取消公告', 409)
    require(not row(db, "SELECT id FROM swaps WHERE shift_id=? AND state='pending'", (shift['id'],)), '该班次已有待确认请求', 409)
    request_id = uid()
    db.execute("INSERT INTO swaps VALUES (?,?,?,?,?,?,?,?,'pending',?,NULL)",(request_id,shift['id'],target_id,member['id'],recipient['id'],shift['version'],target_version,text_field(body.get('reason',''),1000),now()))
    if not public_selection:
        swap_mail.queue_request(db,request_id)
    audit(db,member['id'],'swap.request',dict(id=request_id,shift=shift['id'],to=recipient['id']))
    message = '换班请求已发起，邮件提醒已排队，等待对方确认。'
    if not mailer.configured(): message = '换班请求已发起；邮件服务尚未配置，提醒已排队但未发送。'
    elif not recipient['email']: message = '换班请求已发起；接手人邮箱尚未绑定，邮件提醒暂不能发送。'
    return dict(id=request_id,email_status='pending',message=message)


def swap_decide(db,member,body):
    request = row(db,'SELECT * FROM swaps WHERE id=?',(body.get('id'),))
    require(request and request['recipient_id']==member['id'],'只能处理发给自己的换班请求',403)
    require(request['state']=='pending','请求已处理',409)
    decision = body.get('decision')
    require(decision in ('accept','reject'),'处理方式无效')
    if decision=='reject':
        db.execute("UPDATE swaps SET state='rejected',decided_at=? WHERE id=?",(now(),request['id']))
        swap_mail.cancel_request(db,request['id'])
        workflow_mail.swap_result(db,request['id'])
        return dict(state='rejected')
    permit(member,'can_register')
    origin = member_by_id(db,request['requester_id'])
    require(can(origin,'can_register'),'原值班人登记权限已改变',409)
    shift = row(db,'SELECT * FROM shifts WHERE id=?',(request['shift_id'],))
    target = row(db,'SELECT * FROM shifts WHERE id=?',(request['target_shift_id'],)) if request['target_shift_id'] else None
    relevant = [shift] + ([target] if target else [])
    valid = shift and shift['active'] and shift['version']==request['shift_version'] and shift['member_id']==request['requester_id']
    if request['target_shift_id']:
        valid = valid and target and target['active'] and target['version']==request['target_version'] and target['member_id']==member['id']
    valid = valid and all(slot_end(s)>dt.datetime.now(TZ) and not row(db,'SELECT id FROM registrations WHERE shift_id=?',(s['id'],)) for s in relevant)
    if not valid:
        db.execute("UPDATE swaps SET state='stale',decided_at=? WHERE id=?",(now(),request['id']))
        swap_mail.cancel_request(db,request['id'])
        workflow_mail.swap_result(db,request['id'])
        return dict(state='stale',message='排班、登记或时间已改变，此请求已失效')
    ids = [s['id'] for s in relevant]
    for s,destination in [(shift,member['id'])] + ([(target,request['requester_id'])] if target else []):
        conflicts = rows(db,'SELECT id FROM shifts WHERE active=1 AND date=? AND slot=? AND member_id=?',(s['date'],s['slot'],destination))
        require(not any(c['id'] not in ids for c in conflicts),'接手人在同一时段已有班次',409)
        db.execute('UPDATE shifts SET member_id=?,version=version+1 WHERE id=?',(destination,s['id']))
        invalidate_mail(db,s['id'])
        db.execute("UPDATE swaps SET state='stale',decided_at=? WHERE state='pending' AND id!=? AND (shift_id=? OR target_shift_id=?)",(now(),request['id'],s['id'],s['id']))
    db.execute("UPDATE swaps SET state='accepted',decided_at=? WHERE id=?",(now(),request['id']))
    workflow_mail.swap_result(db,request['id'])
    audit(db,member['id'],'swap.accept',dict(id=request['id']))
    return dict(state='accepted')


def handoff_create(db,member,body):
    return handoff.create(db,member,body)


def handoff_decide(db,member,body):
    return handoff.decide(db,member,body)


def progress_add(db,member,body):
    permit(member,'can_register')
    task = row(db,'SELECT t.*,r.state FROM tasks t JOIN registrations r ON r.id=t.registration_id WHERE t.id=? AND t.active=1',(body.get('task_id'),))
    require(task and task['assignee_id']==member['id'],'只能追加自己当前负责事务的进展',403)
    require(task['state']=='submitted','请先提交登记')
    text, status = text_field(body.get('text',''),4000),body.get('status')
    require(status in ('pending','done'),'请选择有效状态')
    progress_id = uid()
    db.execute('INSERT INTO progress VALUES (?,?,?,?,?,?)',(progress_id,task['id'],member['id'],text,status,now()))
    workflow_mail.progress(db,task['id'],False,member['id'],progress_id,text,status)
    db.execute('UPDATE tasks SET status=? WHERE id=?',(status,task['id']))
    db.execute('UPDATE registrations SET version=version+1,updated_at=? WHERE id=?',(now(),task['registration_id']))
    audit(db,member['id'],'task.progress',dict(task_id=task['id'],status=status))
    return dict(ok=True)


def message_send(db,member,body):
    recipients = body.get('recipients',[])
    announcement = body.get('announcement',False)
    require(isinstance(announcement,bool),'公告参数无效')
    if announcement:
        permit(member,'can_send_announcements')
        recipients = [m['id'] for m in rows(db,'SELECT id FROM members WHERE active=1 AND id!=?',(member['id'],))]
    require(isinstance(recipients,list) and 1 <= len(recipients) <= 10 and len(set(recipients))==len(recipients),'请选择 1 至 10 位不同收件人')
    for recipient in recipients:
        member_by_id(db,recipient)
    text = text_field(body.get('body',''))
    require(bool(text),'通知内容不能为空')
    if announcement: text='【组公告】\n'+text
    registration_id = body.get('registration_id') or None
    if registration_id:
        record = row(db,'SELECT * FROM registrations WHERE id=?',(registration_id,))
        require(record and can_read_record(db,member,record),'无权关联此记录',403)
        require(record['state']=='submitted','不能关联草稿')
        require(setting(db,'share_records') or all(r==record['author_id'] for r in recipients),'组内记录共享尚未开启，收件人无法查看该关联记录')
    reply_to = body.get('reply_to') or None
    if reply_to:
        original = row(db,'SELECT * FROM messages WHERE id=?',(reply_to,))
        require(original and (original['sender_id']==member['id'] or row(db,'SELECT message_id FROM message_recipients WHERE message_id=? AND member_id=?',(reply_to,member['id']))),'无权回复此通知',403)
    message_id = uid()
    db.execute('INSERT INTO messages VALUES (?,?,?,?,?,?)',(message_id,member['id'],text,registration_id,reply_to,now()))
    for recipient in recipients:
        db.execute('INSERT INTO message_recipients(message_id,member_id) VALUES (?,?)',(message_id,recipient))
    audit(db,member['id'],'message.send',dict(id=message_id,recipients=recipients))
    return dict(id=message_id)


def message_mark(db,member,body):
    require(body.get('kind') in ('read','confirm'),'状态无效')
    recipient = row(db,'SELECT * FROM message_recipients WHERE message_id=? AND member_id=?',(body.get('id'),member['id']))
    require(recipient,'通知不属于当前账号',403)
    db.execute('UPDATE message_recipients SET read_at=COALESCE(read_at,?),confirmed_at=COALESCE(confirmed_at,?) WHERE message_id=? AND member_id=?',(now(),now() if body['kind']=='confirm' else None,body['id'],member['id']))
    return dict(ok=True)


def messages_seen(db,member,body):
    ids = body.get('ids')
    require(isinstance(ids,list) and len(ids)<=100 and all(isinstance(x,str) for x in ids),'通知列表无效')
    for ident in set(ids):
        db.execute('UPDATE message_recipients SET read_at=COALESCE(read_at,?) WHERE message_id=? AND member_id=?',(now(),ident,member['id']))
    return dict(ok=True)


def settings_save(db,member,body):
    permit(member,'can_manage_members')
    if 'escalation_recipient' in body:
        target=body['escalation_recipient'] or None
        require(target is None or isinstance(target,str),'汇总收件人格式错误')
        if target:require(can(member_by_id(db,target),'can_manage_members'),'汇总收件人必须是管理员')
        db.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',('escalation_recipient',js(target)))
        db.execute("UPDATE outbox SET status='canceled',error='汇总收件人设置改变' WHERE kind='reminder_escalation' AND status IN ('pending','failed')")
    for key in ('share_records','share_schedule','ai_consent'):
        if key in body:
            require(isinstance(body[key],bool),'设置格式错误')
            db.execute('UPDATE settings SET value=? WHERE key=?',(js(body[key]),key))
    if 'activation_date' in body:
        value = valid_date(body['activation_date']) if body['activation_date'] else None
        old = setting(db,'activation_date')
        require(not value or value >= now()[:10] or (old and value >= old),'不支持在普通设置中追催历史班次，请选择今天或以后')
        db.execute('UPDATE settings SET value=? WHERE key=?',(js(value),'activation_date'))
        db.execute("UPDATE outbox SET status='canceled',error='催填启用范围改变' WHERE kind IN ('reminder','reminder_escalation') AND status IN ('pending','failed')")
    if 'public_swap_mail_mode' in body:
        mode = body['public_swap_mail_mode']
        require(mode in ('off','test','all'), '公开换班邮件范围无效')
        db.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', ('public_swap_mail_mode',js(mode)))
    audit(db,member['id'],'settings.save',body)
    return dict(ok=True)


def member_save(db,member,body):
    permit(member,'can_manage_members')
    target = member_by_id(db,body.get('id'))
    email = text_field(body.get('email',''),254).lower() or None
    require(not email or re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email),'邮箱格式错误')
    caps = body.get('caps')
    require(isinstance(caps,list) and all(c in CAPS for c in caps) and len(set(caps))==len(caps),'权限格式错误')
    if 'can_manage_members' in json.loads(target['caps']) and 'can_manage_members' not in caps:
        require(any(m['id']!=target['id'] and 'can_manage_members' in json.loads(m['caps']) for m in rows(db,'SELECT id,caps FROM members WHERE active=1')),'必须保留至少一位成员管理员')
    changed = email != target['email'] or set(caps) != set(json.loads(target['caps']))
    if changed:
        db.execute('DELETE FROM sessions WHERE member_id=?',(target['id'],))
        if target['email']: db.execute('DELETE FROM auth_codes WHERE email=?',(target['email'],))
    db.execute('UPDATE members SET email=?,caps=? WHERE id=?',(email,js(caps),target['id']))
    audit(db,member['id'],'member.configure',dict(id=target['id'],caps=caps,email_changed=email!=target['email']))
    return dict(ok=True,reauthenticate=changed and target['id']==member['id'])


def schedule_adjust(db,member,body):
    permit(member,'can_manage_schedule')
    shift = row(db,'SELECT * FROM shifts WHERE id=?',(body.get('id'),))
    require(shift and shift['active'],'班次不存在',404)
    require(body.get('version')==shift['version'],'排班已改变',409)
    require(slot_end(shift)>dt.datetime.now(TZ),'历史班次需要单独审计更正，不能直接修改')
    require(not row(db,'SELECT id FROM registrations WHERE shift_id=?',(shift['id'],)),'已有登记或草稿的班次不能直接修改')
    destination = member_by_id(db,body.get('member_id'))
    require(can(destination,'can_register'),'接手人没有登记权限')
    reason = text_field(body.get('reason',''),1000)
    db.execute('UPDATE shifts SET member_id=?,version=version+1 WHERE id=?',(destination['id'],shift['id']))
    invalidate_mail(db,shift['id'])
    db.execute("UPDATE swaps SET state='stale',decided_at=? WHERE state='pending' AND (shift_id=? OR target_shift_id=?)",(now(),shift['id'],shift['id']))
    audit(db,member['id'],'schedule.adjust',dict(shift=shift['id'],previous=shift['member_id'],current=destination['id'],reason=reason))
    return dict(ok=True)


def export_records(db,member,filters):
    require(can(member,'can_export') or can(member,'can_manage_members'),'没有导出权限',403)
    records = state(db,member,filters)['records']
    output = []
    for record in records:
        slot = SLOTS[record['slot']]['name'] if record['slot'] is not None else '值班时间外'
        tasks = record['tasks']
        content = '\n'.join(f'{i+1}. {t["text"]}' for i,t in enumerate(tasks))
        complete = '不适用' if not tasks else ('是' if all(t['status']=='done' for t in tasks) else '否')
        output.append([record['author_name'],record['date'],slot,content,complete,record['notes'],'已提交' if record['state']=='submitted' else '草稿',record['submitted_at'] or ''])
    return workbook(['姓名','事务日期','工作时段','事务内容','是否完成','其他内容补充','登记状态','首次提交时间'],output)


def prepare_export(db,member,body):
    from urllib.parse import urlencode
    require(can(member,'can_export') or can(member,'can_manage_members'),'没有导出权限',403)
    filters = {k:body[k] for k in ('from','to','author','slot','q','state','task_status') if body.get(k)}
    require(all(isinstance(v,str) for v in filters.values()),'导出筛选格式无效')
    filters.setdefault('state','submitted')
    count = len(state(db,member,filters)['records'])
    return dict(download='/api/export?'+urlencode(filters),count=count)


ACTIONS = {'preferences/home-order':save_home_order,'records/save':save_record,'imports/preview':import_preview,'imports/confirm':import_confirm,
           'records/import-preview':record_import.prepare,'records/import-confirm':record_import.confirm,
           'records/admin-save':admin_save_record,'exports/prepare':prepare_export,
           'public-swaps/create':public_swap.create,'public-swaps/offer':public_swap.offer,'public-swaps/withdraw':public_swap.withdraw,'public-swaps/cancel':public_swap.cancel,'public-swaps/select':public_swap.select,
           'swaps/create':swap_create,'swaps/decide':swap_decide,'handoffs/create':handoff_create,
           'handoffs/decide':handoff_decide,'handoffs/comment':handoff.comment,'handoffs/progress':handoff.direct_progress,'tasks/follow':handoff.follow,'tasks/progress':progress_add,'messages/send':message_send,
           'messages/mark':message_mark,'messages/seen':messages_seen,'settings/save':settings_save,'members/save':member_save,'schedule/adjust':schedule_adjust}
