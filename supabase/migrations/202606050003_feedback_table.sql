-- Durable store for in-app feedback so "Send" always works, even before a
-- GITHUB_TOKEN is configured. The submit-feedback edge function writes here with
-- the service role (bypassing RLS) and, when a token is set, also opens a GitHub
-- issue. No public access - the founder reads it from the dashboard.
create table if not exists public.feedback (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid references auth.users(id) on delete set null,
  email       text,
  message     text not null,
  image_url   text,
  app_version text,
  category    text default 'feedback',
  issue_url   text,
  created_at  timestamptz not null default now()
);

alter table public.feedback enable row level security;
-- Only the service role (edge function) may read/write; deny everyone else.
revoke all on public.feedback from anon, authenticated;
