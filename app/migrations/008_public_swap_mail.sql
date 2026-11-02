CREATE TABLE public_swap_mail_links (
 outbox_id TEXT PRIMARY KEY REFERENCES outbox(id),
 public_id TEXT NOT NULL REFERENCES public_swaps(id),
 offer_id TEXT REFERENCES public_swap_offers(id),
 event TEXT NOT NULL CHECK(event IN ('published','offer','selected','not_selected'))
);
INSERT OR IGNORE INTO settings(key,value) VALUES ('public_swap_mail_mode','"off"');
