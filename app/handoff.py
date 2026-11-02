"""Personal follow-ups and participant-only handoff conversations."""
from . import workflow_mail
from .core import audit, can, member_by_id, now, permit, require, row, rows, text_field, uid


def personal_tasks(db, member):
    tasks = rows(db, '''SELECT t.*,r.date,r.id AS record_id,m.name AS author_name,'record' AS kind,
        CASE WHEN EXISTS(SELECT 1 FROM record_import_sources i WHERE i.registration_id=r.id)
        AND NOT EXISTS(SELECT 1 FROM task_followups f WHERE f.task_id=t.id)
        AND NOT EXISTS(SELECT 1 FROM handoffs h WHERE h.task_id=t.id AND h.state='accepted') THEN 1 ELSE 0 END AS historical
        FROM tasks t JOIN registrations r ON r.id=t.registration_id JOIN members m ON m.id=r.author_id
        WHERE t.active=1 AND t.status='pending' AND t.assignee_id=? AND r.state='submitted'
        ORDER BY r.date DESC,t.id''', (member['id'],))
    direct = rows(db, '''SELECT t.*,substr(t.created_at,1,10) AS date,m.name AS author_name,'direct' AS kind,
        0 AS historical FROM direct_tasks t JOIN members m ON m.id=t.creator_id
        WHERE t.assignee_id=? AND t.status='pending' ORDER BY t.created_at DESC''', (member['id'],))
    return dict(todos=[t for t in tasks if not t['historical']] + direct,
                historical_tasks=[t for t in tasks if t['historical']])


def follow(db, member, body):
    permit(member, 'can_register')
    task = row(db, '''SELECT t.* FROM tasks t JOIN registrations r ON r.id=t.registration_id
        WHERE t.id=? AND t.active=1 AND r.state='submitted' ''', (body.get('task_id'),))
    require(task and task['assignee_id']==member['id'], '只能跟进自己负责的事务', 403)
    require(task['status']=='pending', '该事务已经完成', 409)
    db.execute('INSERT OR REPLACE INTO task_followups VALUES (?,?,?)', (task['id'],member['id'],now()))
    audit(db, member['id'], 'task.follow', dict(task_id=task['id']))
    return dict(ok=True)


def threads(db, member):
    result = rows(db, '''SELECT h.*,a.name AS sender_name,b.name AS recipient_name,
        COALESCE(t.text,d.text) AS text,COALESCE(t.status,d.status) AS task_status
        FROM handoffs h LEFT JOIN tasks t ON t.id=h.task_id LEFT JOIN direct_tasks d ON d.id=h.direct_task_id
        JOIN members a ON a.id=h.sender_id JOIN members b ON b.id=h.recipient_id
        WHERE h.sender_id=? OR h.recipient_id=? ORDER BY h.created_at DESC LIMIT 200''', (member['id'],member['id']))
    for h in result:
        h['comments'] = rows(db, '''SELECT c.*,m.name AS author_name FROM handoff_comments c
            JOIN members m ON m.id=c.author_id WHERE c.handoff_id=? ORDER BY c.created_at,c.rowid''', (h['id'],))
    return result


def create(db, member, body):
    permit(member, 'can_register')
    recipient = member_by_id(db, body.get('recipient_id'))
    require(recipient['id']!=member['id'] and can(recipient,'can_register'), '请选择另一位可登记成员')
    reason = text_field(body.get('reason',''),1000)
    task_id, direct_id = body.get('task_id') or None, body.get('direct_task_id') or None
    require(not (task_id and direct_id), '只能选择一个交接事项')
    if task_id:
        task = row(db, '''SELECT t.*,r.state FROM tasks t JOIN registrations r ON r.id=t.registration_id
            WHERE t.id=? AND t.active=1''', (task_id,))
        require(task and task['assignee_id']==member['id'], '只能交接当前由自己处理的事务',403)
        require(task['state']=='submitted' and task['status']=='pending', '只能交接已登记的未完成事务')
        require(not row(db,"SELECT id FROM handoffs WHERE task_id=? AND state='pending'",(task_id,)), '这项事务已有待处理的交接请求',409)
        db.execute('INSERT OR REPLACE INTO task_followups VALUES (?,?,?)',(task_id,member['id'],now()))
    elif direct_id:
        task = row(db,'SELECT * FROM direct_tasks WHERE id=?',(direct_id,))
        require(task and task['assignee_id']==member['id'], '只能交接当前由自己处理的事务',403)
        require(task['status']=='pending', '已完成的事务不能交接',409)
        require(not row(db,"SELECT id FROM handoffs WHERE direct_task_id=? AND state='pending'",(direct_id,)), '这项事务已有待处理的交接请求',409)
    else:
        text = text_field(body.get('text',''),4000)
        require(text, '请填写交接事项')
        direct_id = uid()
        db.execute('INSERT INTO direct_tasks VALUES (?,?,?,?,?,?)',(direct_id,text,member['id'],member['id'],'pending',now()))
    request_id = uid()
    db.execute("INSERT INTO handoffs(id,task_id,sender_id,recipient_id,reason,state,created_at,direct_task_id) VALUES (?,?,?,?,?,'pending',?,?)",
               (request_id,task_id,member['id'],recipient['id'],reason,now(),direct_id))
    workflow_mail.handoff_event(db,request_id,'created',member['id'])
    audit(db,member['id'],'handoff.create',dict(id=request_id,task_id=task_id,direct_task_id=direct_id))
    return dict(id=request_id)


def decide(db, member, body):
    h = row(db,'SELECT * FROM handoffs WHERE id=?',(body.get('id'),))
    require(h and h['recipient_id']==member['id'], '只能处理发给自己的交接',403)
    require(h['state']=='pending', '交接已处理',409)
    require(body.get('decision') in ('accept','reject'), '处理方式无效')
    decision = 'accepted' if body['decision']=='accept' else 'rejected'
    if decision=='accepted':
        permit(member,'can_register')
        table = 'tasks' if h['task_id'] else 'direct_tasks'
        task_id = h['task_id'] or h['direct_task_id']
        task = row(db,f'SELECT * FROM {table} WHERE id=?',(task_id,))
        if not task or not task.get('active',1) or task['assignee_id']!=h['sender_id'] or task['status']!='pending':
            decision='stale'
        else:
            db.execute(f'UPDATE {table} SET assignee_id=? WHERE id=?',(member['id'],task_id))
            if table=='tasks':
                db.execute('UPDATE registrations SET version=version+1,updated_at=? WHERE id=?',(now(),task['registration_id']))
    db.execute('UPDATE handoffs SET state=?,decided_at=? WHERE id=?',(decision,now(),h['id']))
    workflow_mail.handoff_event(db,h['id'],decision,member['id'])
    audit(db,member['id'],'handoff.decide',dict(id=h['id'],state=decision))
    return dict(state=decision)


def comment(db, member, body):
    h = row(db,'SELECT * FROM handoffs WHERE id=?',(body.get('id'),))
    require(h and member['id'] in (h['sender_id'],h['recipient_id']), '只能在自己参与的交接中留言',403)
    text = text_field(body.get('text',''),4000)
    require(text, '留言不能为空')
    comment_id = uid()
    db.execute('INSERT INTO handoff_comments VALUES (?,?,?,?,?)',(comment_id,h['id'],member['id'],text,now()))
    workflow_mail.handoff_event(db,h['id'],'comment',member['id'],comment_id,text)
    audit(db,member['id'],'handoff.comment',dict(id=h['id']))
    return dict(ok=True)


def direct_progress(db, member, body):
    permit(member,'can_register')
    task = row(db,'SELECT * FROM direct_tasks WHERE id=?',(body.get('task_id'),))
    require(task and task['assignee_id']==member['id'], '只能更新自己当前负责的事务',403)
    text, status = text_field(body.get('text',''),4000),body.get('status')
    require(status in ('pending','done'), '请选择有效状态')
    progress_id = uid()
    db.execute('INSERT INTO direct_progress VALUES (?,?,?,?,?,?)',(progress_id,task['id'],member['id'],text,status,now()))
    workflow_mail.progress(db,task['id'],True,member['id'],progress_id,text,status)
    db.execute('UPDATE direct_tasks SET status=? WHERE id=?',(status,task['id']))
    # Every participant in this task's handoff chain can see the status update.
    for h in rows(db,'SELECT id FROM handoffs WHERE direct_task_id=?',(task['id'],)):
        db.execute('INSERT INTO handoff_comments VALUES (?,?,?,?,?)',(uid(),h['id'],member['id'],(('已完成：' if status=='done' else '进展：')+text if text else ('已完成' if status=='done' else '更新为尚未完成')),now()))
    audit(db,member['id'],'handoff.progress',dict(task_id=task['id'],status=status))
    return dict(ok=True)
