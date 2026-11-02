"""Durable request notifications, separate from shift-registration reminders."""
import datetime as dt
import os

from . import notifications
from .core import SLOTS, can, now, row, slot_end, uid


def queue_request(db, request_id):
    request = row(db, '''SELECT w.*,a.name AS requester_name,b.name AS recipient_name
        FROM swaps w JOIN members a ON a.id=w.requester_id
        JOIN members b ON b.id=w.recipient_id WHERE w.id=?''', (request_id,))
    shift = row(db, 'SELECT * FROM shifts WHERE id=?', (request['shift_id'],))
    target = row(db, 'SELECT * FROM shifts WHERE id=?', (request['target_shift_id'],)) if request['target_shift_id'] else None

    def describe(s):
        slot = SLOTS[s['slot']]
        return f"{s['date']} {slot['name']}（{slot['start']}—{slot['end']}，北京时间）"

    kind = '双方对调' if target else '单次代班'
    url = os.getenv('APP_URL', 'http://127.0.0.1:8765').rstrip('/') + '/#messages?tab=swaps&request=' + request_id
    body = (f"{request['recipient_name']}，{request['requester_name']} 向你发起了{kind}请求。\n"
            f"对方班次：{describe(shift)}\n")
    if target: body += f"你的对调班次：{describe(target)}\n"
    body += (f"说明：{request['reason'] or '未填写'}\n"
             f"请登录工作台查看并接受或拒绝：{url}\n"
             "接受前原排班继续有效；接受后网站实际值班表与登记责任同步更新。\n"
             "请以工作台中的最新状态为准，这封邮件不代表换班已生效。")
    notifications.emit(db,request['requester_id'],[request['recipient_id']],'swap-request:'+request_id,body,'#messages?tab=swaps&request='+request_id)
    outbox_id = uid()
    db.execute('''INSERT INTO outbox(id,dedupe_key,kind,recipient_id,subject,body,status,next_attempt_at,created_at)
        VALUES (?,?,'swap_request',?,?,?,'pending',?,?)''',
        (outbox_id,'swap-request:'+request_id,request['recipient_id'],
         f"换班请求 · {request['requester_name']} · {shift['date']} {SLOTS[shift['slot']]['name']}",body,now(),now()))
    db.execute('INSERT INTO swap_mail_links VALUES (?,?)', (outbox_id,request_id))


def cancel_request(db, request_id):
    db.execute('''UPDATE outbox SET status='canceled',error='换班请求已处理或失效'
        WHERE status IN ('pending','failed') AND id IN
        (SELECT outbox_id FROM swap_mail_links WHERE swap_id=?)''', (request_id,))


def eligible(db, item, at):
    request = row(db, '''SELECT w.* FROM swaps w JOIN swap_mail_links l ON l.swap_id=w.id
        WHERE l.outbox_id=?''', (item['id'],))
    if not request or request['state']!='pending' or request['recipient_id']!=item['recipient_id']:
        return False
    for member_id in (request['requester_id'],request['recipient_id']):
        member = row(db,'SELECT * FROM members WHERE id=? AND active=1',(member_id,))
        if not member or not can(member,'can_register'): return False
    checks = [(request['shift_id'],request['shift_version'],request['requester_id'])]
    if request['target_shift_id']:
        checks.append((request['target_shift_id'],request['target_version'],request['recipient_id']))
    for shift_id,version,member_id in checks:
        shift = row(db,'SELECT * FROM shifts WHERE id=?',(shift_id,))
        if (not shift or not shift['active'] or shift['version']!=version or shift['member_id']!=member_id
                or slot_end(shift)<=at or row(db,'SELECT id FROM registrations WHERE shift_id=?',(shift_id,))):
            return False
    return True
