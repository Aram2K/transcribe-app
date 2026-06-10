-- Pro accounts + entitlements for Transcribe.
-- profiles (1:1 with auth.users) and subscriptions (synced from Stripe by a
-- service-role webhook only). RLS lets users read only their own rows; there are
-- deliberately NO client write policies, so a client can never grant itself Pro.

create table if not exists public.profiles (
  id                 uuid primary key references auth.users(id) on delete cascade,
  email              text,
  stripe_customer_id text unique,
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now()
);

create table if not exists public.subscriptions (
  id                   text primary key,            -- Stripe subscription id (sub_...)
  user_id              uuid not null references auth.users(id) on delete cascade,
  status               text not null,               -- active | trialing | past_due | canceled | ...
  price_id             text,
  plan                 text,                        -- 'monthly' | 'annual'
  current_period_end   timestamptz,
  cancel_at_period_end boolean not null default false,
  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now()
);
create index if not exists subscriptions_user_id_idx on public.subscriptions(user_id);
create index if not exists subscriptions_status_idx  on public.subscriptions(status);

alter table public.profiles      enable row level security;
alter table public.subscriptions enable row level security;

drop policy if exists "read own profile" on public.profiles;
create policy "read own profile" on public.profiles
  for select using ((select auth.uid()) = id);

drop policy if exists "read own subscriptions" on public.subscriptions;
create policy "read own subscriptions" on public.subscriptions
  for select using ((select auth.uid()) = user_id);

-- Server-side entitlement truth. The app calls this RPC.
create or replace function public.is_pro()
returns boolean
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select exists (
    select 1
    from public.subscriptions s
    where s.user_id = (select auth.uid())
      and s.status in ('active', 'trialing')
      and (s.current_period_end is null or s.current_period_end > now())
  );
$$;

revoke all on function public.is_pro() from public, anon;
grant execute on function public.is_pro() to authenticated;

create or replace function public.my_entitlement()
returns table (is_pro boolean, plan text, status text, current_period_end timestamptz, cancel_at_period_end boolean)
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select
    (s.status in ('active','trialing') and (s.current_period_end is null or s.current_period_end > now())) as is_pro,
    s.plan, s.status, s.current_period_end, s.cancel_at_period_end
  from public.subscriptions s
  where s.user_id = (select auth.uid())
  order by s.current_period_end desc nulls last
  limit 1;
$$;

revoke all on function public.my_entitlement() from public, anon;
grant execute on function public.my_entitlement() to authenticated;

-- Auto-create a profile row when a user signs up.
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
begin
  insert into public.profiles (id, email)
  values (new.id, new.email)
  on conflict (id) do nothing;
  return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();
