"""Visible, participant-scoped in-app activity with ordinary read receipts."""
from .core import now, row, uid


def emit(db, actor_id, recipients, event_key, body, target_url):
    if row(db, 'SELECT message_id FROM notification_links WHERE event_key=?', (event_key,)):
        return
    recipients = set(recipients) - {actor_id}
    recipients = {m for m in recipients if row(db, 'SELECT id FROM members WHERE id=? AND active=1', (m,))}
    if not recipients:
        return
    ident = uid()
    db.execute('INSERT INTO messages VALUES (?,?,?,?,?,?)', (ident, actor_id, body, None, None, now()))
    db.execute('INSERT INTO notification_links VALUES (?,?,?)', (ident, event_key, target_url))
    for recipient in recipients:
        db.execute('INSERT INTO message_recipients(message_id,member_id) VALUES (?,?)', (ident, recipient))
