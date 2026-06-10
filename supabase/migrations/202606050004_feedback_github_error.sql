-- Per-row diagnostic: if opening the GitHub issue fails (bad/expired token,
-- missing Issues permission, repo renamed, GitHub down), the submit-feedback
-- function records the GitHub status + detail here. null = success (or no token).
-- The feedback itself is always saved regardless; this just aids debugging.
alter table public.feedback add column if not exists github_error text;
