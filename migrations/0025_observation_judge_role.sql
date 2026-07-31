-- The role behind the observe-hook judgment, corrected in both respects it was wrong in:
-- its slug ('tool_judge' becomes 'tool_observation_judge') and its rung (mid becomes small).
--
-- The slug is durable, so renaming it in code alone would leave two rows behind on any box that has booted:
-- the dead 'tool_judge' nobody resolves any more, seeded on some earlier startup,
-- and the new one the next reconcile adds.
-- The /models command lists what the table holds, so the dead row would show up there as a role to assign,
-- which is the opposite of what the rename was for.
-- An UPDATE rather than a DELETE and a re-seed, because an operator may have moved this role off its default,
-- and a rename must not quietly hand the judgment back to a tier they did not choose.
-- Guarded so it is a no-op wherever the old row was never written (a fresh database, or one that has
-- already been through this), and the DELETE clears the old row in the one case where both somehow exist.
UPDATE model_role SET role = 'tool_observation_judge'
WHERE role = 'tool_judge'
  AND NOT EXISTS (SELECT 1 FROM model_role WHERE role = 'tool_observation_judge');

DELETE FROM model_role WHERE role = 'tool_judge';

-- The rung. This judgment is a bounded pick — a handful of lines and one question, answered with a ref or a null,
-- with the reply schema admitting only the refs the hook offered — so it belongs on the small tier
-- beside the router's re-rank, not on the mid tier beside the argument extraction and the spoken confirmation.
-- The new seed default says so (config.TOOL_OBSERVATION_JUDGE_MODEL), but a seed only ever writes a role
-- that has no row yet, so a box that already booted keeps the mid model it was seeded with:
-- this moves it. Conditioned on the mid model it was actually seeded with,
-- so an operator's own assignment is left exactly where they put it,
-- and on the small model being in the catalog, the same care the seed takes against a foreign-key violation.
UPDATE model_role SET model_name = 'gemini-3.5-flash-lite'
WHERE role = 'tool_observation_judge'
  AND model_name = 'gemini-3.6-flash'
  AND EXISTS (SELECT 1 FROM model WHERE name = 'gemini-3.5-flash-lite');
