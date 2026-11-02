CREATE TABLE public_swaps (
 id TEXT PRIMARY KEY, requester_id TEXT NOT NULL REFERENCES members(id),
 shift_id TEXT NOT NULL REFERENCES shifts(id), shift_version INTEGER NOT NULL,
 reason TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open','matched','canceled','stale')),
 created_at TEXT NOT NULL, decided_at TEXT, swap_id TEXT REFERENCES swaps(id)
);
CREATE UNIQUE INDEX public_swap_open_shift ON public_swaps(shift_id) WHERE state='open';
CREATE TABLE public_swap_offers (
 id TEXT PRIMARY KEY, public_id TEXT NOT NULL REFERENCES public_swaps(id),
 member_id TEXT NOT NULL REFERENCES members(id), target_shift_id TEXT REFERENCES shifts(id),
 target_version INTEGER, message TEXT NOT NULL, created_at TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','selected','not_selected','withdrawn','stale'))
);
CREATE UNIQUE INDEX public_swap_active_offer ON public_swap_offers(public_id,member_id) WHERE state='active';
