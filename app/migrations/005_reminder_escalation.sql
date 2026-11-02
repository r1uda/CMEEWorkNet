CREATE TABLE reminder_escalation_items (
 outbox_id TEXT NOT NULL REFERENCES outbox(id),
 shift_id TEXT NOT NULL REFERENCES shifts(id),
 shift_version INTEGER NOT NULL,
 member_id TEXT NOT NULL REFERENCES members(id),
 PRIMARY KEY(outbox_id,shift_id)
);
CREATE INDEX escalation_shift ON reminder_escalation_items(shift_id);
