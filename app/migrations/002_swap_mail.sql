CREATE TABLE swap_mail_links (
 outbox_id TEXT PRIMARY KEY REFERENCES outbox(id),
 swap_id TEXT NOT NULL UNIQUE REFERENCES swaps(id)
);
