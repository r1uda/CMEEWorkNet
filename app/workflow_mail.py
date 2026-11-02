"""Transactional notifications for each participant-facing workflow event."""
import os

from . import notifications
from .core import SLOTS, now, row, rows, uid


def queue(db, entity, ident, event, event_id, recipients, subject, body, actor_id=None):
    query = {'swap': 'tab=swaps&request=', 'handoff': 'tab=handoffs&handoff=',
             'public': 'tab=swaps&view=public&public='}[entity]
    url = os.getenv('APP_URL', 'http://127.0.0.1:8765').rstrip('/') + '/#messages?' + query + ident
    if actor_id:
        notifications.emit(db,actor_id,recipients,f'{entity}:{ident}:{event}:{event_id}',subject+'\n'+body,'#messages?'+query+ident)
    body += '\n\n打开工作台查看：' + url + '\n请使用本人账号登录，以网站最新状态为准；请在网站处理，不要直接回复此邮件。'
    for recipient in set(recipients):
        if not row(db, 'SELECT id FROM members WHERE id=? AND active=1', (recipient,)):
            continue
        if entity == 'public':
            from .public_swap_mail import enabled
            if not enabled(db, recipient, ident):
                continue
        outbox_id = uid()
        inserted = db.execute('''INSERT OR IGNORE INTO outbox
            (id,dedupe_key,kind,recipient_id,subject,body,status,next_attempt_at,created_at)
            VALUES (?,?,'workflow',?,?,?,'pending',?,?)''',
            (outbox_id, f'workflow:{entity}:{ident}:{event}:{event_id}:{recipient}',
             recipient, subject, body, now(), now())).rowcount
        if inserted:
            db.execute('INSERT INTO workflow_mail_links VALUES (?,?,?,?)', (outbox_id, entity, ident, event))


def swap_result(db, ident):
    # Public selection uses the same swap implementation but its own result mail.
    if not row(db, 'SELECT outbox_id FROM swap_mail_links WHERE swap_id=?', (ident,)):
        return
    w = row(db, '''SELECT w.*,m.name,s.date,s.slot FROM swaps w JOIN members m ON m.id=w.recipient_id
        JOIN shifts s ON s.id=w.shift_id WHERE w.id=?''', (ident,))
    label = {'accepted': '同意', 'rejected': '拒绝', 'stale': '请求失效'}[w['state']]
    body = f"{w['name']} 对你的换班请求的处理结果：{label}。\n班次：{w['date']} {SLOTS[w['slot']]['name']}（北京时间）。\n"
    body += '实际值班表已更新，请按最新排班值班。' if w['state'] == 'accepted' else '此次换班未生效，请查看最新排班。'
    queue(db, 'swap', ident, w['state'], w['state'], [w['requester_id']], '换班结果 · ' + label, body, actor_id=w['recipient_id'])


def handoff_event(db, ident, event, actor_id, event_id=None, detail=''):
    h = row(db, '''SELECT h.*,COALESCE(t.text,d.text) AS text FROM handoffs h
        LEFT JOIN tasks t ON t.id=h.task_id LEFT JOIN direct_tasks d ON d.id=h.direct_task_id
        WHERE h.id=?''', (ident,))
    actor = row(db, 'SELECT name FROM members WHERE id=?', (actor_id,))['name']
    label = {'created': '发起交接', 'accepted': '接受交接', 'rejected': '拒绝交接',
             'stale': '交接失效', 'comment': '新增留言', 'progress': '更新进展', 'done': '事务已完成'}[event]
    recipients = {h['sender_id'], h['recipient_id']} - {actor_id}
    body = f"{actor}：{label}\n交接事项：{h['text']}\n"
    if event == 'created':
        body += f"说明：{h['reason'] or '未填写'}\n请在网站接受或拒绝；接受后才转移处理责任。\n"
    if detail:
        body += detail
    queue(db, 'handoff', ident, event, event_id or event, recipients, '事务交接 · ' + label, body, actor_id=actor_id)


def progress(db, task_id, direct, actor_id, event_id, text, status):
    # Notify each counterpart once even when a task has several handoff attempts.
    column = 'direct_task_id' if direct else 'task_id'
    notified = set()
    for h in rows(db, f'SELECT * FROM handoffs WHERE {column}=? ORDER BY created_at DESC,rowid DESC', (task_id,)):
        recipients = {h['sender_id'], h['recipient_id']} - {actor_id} - notified
        if not recipients:
            continue
        actor = row(db, 'SELECT name FROM members WHERE id=?', (actor_id,))['name']
        label = '事务已完成' if status == 'done' else '更新进展'
        queue(db, 'handoff', h['id'], 'progress', event_id, recipients, '事务交接 · ' + label,
              f'{actor}：{label}\n{text}', actor_id=actor_id)
        notified.update(recipients)


def eligible(db, item, at):
    link = row(db, 'SELECT * FROM workflow_mail_links WHERE outbox_id=?', (item['id'],))
    if not link or not row(db, 'SELECT id FROM members WHERE id=? AND active=1', (item['recipient_id'],)):
        return False
    if link['entity'] == 'swap':
        w = row(db, 'SELECT * FROM swaps WHERE id=?', (link['entity_id'],))
        return bool(w and w['requester_id'] == item['recipient_id'] and w['state'] == link['event'])
    if link['entity'] == 'handoff':
        h = row(db, 'SELECT * FROM handoffs WHERE id=?', (link['entity_id'],))
        # Historical event mail remains valid even if a later action happened first.
        return bool(h and item['recipient_id'] in (h['sender_id'], h['recipient_id']))
    from .public_swap_mail import enabled
    p = row(db, 'SELECT * FROM public_swaps WHERE id=?', (link['entity_id'],))
    return bool(p and enabled(db, item['recipient_id'], link['entity_id']))
