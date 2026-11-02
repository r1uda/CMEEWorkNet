ALTER TABLE public_swap_offers ADD COLUMN reply_kind TEXT NOT NULL DEFAULT 'cover' CHECK(reply_kind IN ('exchange','cover','undecided'));
UPDATE public_swap_offers SET reply_kind='exchange' WHERE target_shift_id IS NOT NULL;
