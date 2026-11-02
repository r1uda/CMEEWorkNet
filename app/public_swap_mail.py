"""Event-specific public swap mail, with an independent, explicit test audience."""
import os

from . import notifications
from .core import SLOTS, now, row, rows, setting, uid

TEST_MEMBERS = ('m03', 'm10')


def enabled(db, member_id, public_id=None):
    audience = setting(db, "public_swap_mail_audience:" + public_id) if public_id else None
    if audience is not None and member_id not in audience:
        return False
    mode = setting(db, 'public_swap_mail_mode')
    return mode == 'all' or (mode == 'test' and member_id in TEST_MEMBERS)


def queue(db, public_id, event, offer_id=None):
    post = row(db, '''SELECT p.*,m.name AS requester_name,s.date,s.slot FROM public_swaps p
        JOIN members m ON m.id=p.requester_id JOIN shifts s ON s.id=p.shift_id WHERE p.id=?''', (public_id,))
    proposal = row(db, '''SELECT o.*,m.name AS member_name,s.date AS target_date,s.slot AS target_slot
        FROM public_swap_offers o JOIN members m ON m.id=o.member_id LEFT JOIN shifts s ON s.id=o.target_shift_id
        WHERE o.id=? AND o.public_id=?''', (offer_id, public_id)) if offer_id else None
    def when(date, slot):
        s = SLOTS[slot]
        return f"{date} {s['name']}（{s['start']}—{s['end']}，北京时间）"
    shift = when(post['date'], post['slot'])
    url = os.getenv('APP_URL', 'http://127.0.0.1:8765').rstrip('/') + '/#messages?tab=swaps&view=public&public=' + public_id
    if event == 'published':
        recipients = [m['id'] for m in rows(db, 'SELECT id FROM members WHERE active=1 AND id!=?', (post['requester_id'],))]
        audience = setting(db, 'public_swap_mail_audience:' + public_id)
        if audience is not None:
            recipients = audience
        title = f"公开求换班 · {post['requester_name']} · {shift}"
        body = f"{post['requester_name']} 发布了公开求换班。\n需要换出的班次：{shift}。\n"
        if post['reason']: body += f"说明：{post['reason']}\n"
        body += '可选择自己的班次交换、选择“班次待定”稍后补选，或仅代班、不交换。成员回复在网站内公开可见；双方确认前，排班不变。'
    elif event == 'offer':
        recipients = [post['requester_id']]
        name = proposal['member_name']
        if proposal['reply_kind'] == 'exchange':
            target = when(proposal['target_date'], proposal['target_slot'])
            title = f"{name} 申请用 {proposal['target_date']} {SLOTS[proposal['target_slot']]['name']} 与你交换"
            body = f"{name} 申请与你交换班次。\n你的班次：{shift}\n对方用于交换的班次：{target}\n确认后，你的班次由 {name} 负责，对方的班次由你负责。\n请到网站点击“接受交换”并核对；确认后才会更新排班。"
        elif proposal['reply_kind'] == 'undecided':
            title = f"{name} 愿意交换，具体班次待定"
            body = f"{name} 回复了你的公开求换班。\n你的班次：{shift}\n对方愿意交换，尚未确定用于交换的班次，将在之后补选。\n当前仅表达换班意愿，不代表同意直接代班，也不会修改排班。请等待对方确定具体班次。"
        else:
            title = f"{name} 申请代班 · {post['date']} {SLOTS[post['slot']]['name']}"
            body = f"{name} 愿意代班。\n代班时间：{shift}\n此次仅代班，不交换对方的班次。\n请到网站点击“接受代班”并核对；确认后才会更新排班。"
        if proposal['message']: body += f"\n对方留言：{proposal['message']}"
    elif event == 'selected':
        recipients = [proposal['member_id']]
        title = f"{post['requester_name']} 已同意与你换班" if proposal['target_shift_id'] else f"{post['requester_name']} 已同意由你代班"
        body = f"{post['requester_name']} 已同意，你将负责 {shift}。\n"
        if proposal['target_shift_id']:
            body += f"你的 {when(proposal['target_date'], proposal['target_slot'])} 已由 {post['requester_name']} 负责。\n"
        body += '网站实际值班表已更新，请按最新排班值班。'
    else:
        recipients = [proposal['member_id']]
        title = f"{post['requester_name']} 已与其他成员确认换班"
        body = f"你回复的公开求换班已结束。\n发起人：{post['requester_name']}\n班次：{shift}\n发起人已与其他成员确认换班。你无需承担该班次，你的排班保持不变。"
    actor_id = proposal['member_id'] if event=='offer' else post['requester_id']
    notifications.emit(db,actor_id,recipients,f'public:{public_id}:{event}:{offer_id or ""}',title+'\n'+body,'#messages?tab=swaps&view=public&public='+public_id)
    body += '\n\n打开工作台查看：' + url + '\n请使用本人账号登录，以网站最新状态为准；请在网站报名或处理，不要直接回复此邮件。'
    count = 0
    for recipient in set(recipients):
        if not enabled(db, recipient, public_id):
            continue
        outbox_id = uid()
        inserted = db.execute('''INSERT OR IGNORE INTO outbox(id,dedupe_key,kind,recipient_id,subject,body,status,next_attempt_at,created_at)
            VALUES (?,?,'public_swap',?,?,?,'pending',?,?)''',
            (outbox_id, f'public-swap:{public_id}:{event}:{offer_id or ""}:{recipient}', recipient, title, body, now(), now())).rowcount
        if inserted:
            db.execute('INSERT INTO public_swap_mail_links VALUES (?,?,?,?)', (outbox_id, public_id, offer_id, event))
            count += 1
    return count


def eligible(db, item, at):
    from .public_swap import available
    link = row(db, 'SELECT * FROM public_swap_mail_links WHERE outbox_id=?', (item['id'],))
    if not link or not enabled(db, item['recipient_id'], link['public_id']):
        return False
    post = row(db, 'SELECT * FROM public_swaps WHERE id=?', (link['public_id'],))
    if not post:
        return False
    proposal = row(db, 'SELECT * FROM public_swap_offers WHERE id=? AND public_id=?', (link['offer_id'], post['id'])) if link['offer_id'] else None
    if link['event'] in ('published', 'offer'):
        if post['state'] != 'open' or not available(db, post['shift_id'], post['requester_id'], post['shift_version']):
            return False
        if link['event'] == 'published':
            return item['recipient_id'] != post['requester_id'] or item['recipient_id'] in (setting(db, 'public_swap_mail_audience:' + post['id']) or [])
        return bool(proposal and proposal['state'] == 'active' and item['recipient_id'] == post['requester_id']
                    and (not proposal['target_shift_id'] or available(db, proposal['target_shift_id'], proposal['member_id'], proposal['target_version'])))
    return bool(post['state'] == 'matched' and proposal and proposal['state'] == link['event']
                and proposal['member_id'] == item['recipient_id'])
