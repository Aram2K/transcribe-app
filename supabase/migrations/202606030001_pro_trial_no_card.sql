-- No-card 3-day Pro trial + name capture.
-- Server-authoritative: the trial window lives on the account (auth.users →
-- profiles), so it can't be reset by clearing local data. One trial per account.

alter table public.profiles
  add column if not exists full_name            text,
  add column if not exists pro_trial_started_at timestamptz,
  add column if not exists pro_trial_ends_at    timestamptz;

-- New signups get a 3-day Pro trial (no card) and we capture their name.
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
begin
  insert into public.profiles (id, email, full_name, pro_trial_started_at, pro_trial_ends_at)
  values (
    new.id,
    new.email,
    new.raw_user_meta_data->>'full_name',
    now(),
    now() + interval '3 days'
  )
  on conflict (id) do nothing;
  return new;
end;
$$;
revoke all on function public.handle_new_user() from public, anon, authenticated;

-- is_pro: an active paid subscription OR an unexpired trial.
create or replace function public.is_pro()
returns boolean
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select
    exists (
      select 1 from public.subscriptions s
      where s.user_id = (select auth.uid())
        and s.status in ('active', 'trialing')
        and (s.current_period_end is null or s.current_period_end > now())
    )
    or exists (
      select 1 from public.profiles p
      where p.id = (select auth.uid())
        and p.pro_trial_ends_at is not null
        and p.pro_trial_ends_at > now()
    );
$$;
revoke all on function public.is_pro() from public, anon;
grant execute on function public.is_pro() to authenticated;

-- my_entitlement: prefer a paid subscription; otherwise reflect the trial.
create or replace function public.my_entitlement()
returns table (is_pro boolean, plan text, status text, current_period_end timestamptz, cancel_at_period_end boolean)
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  with sub as (
    select *
    from public.subscriptions s
    where s.user_id = (select auth.uid())
      and s.status in ('active', 'trialing')
      and (s.current_period_end is null or s.current_period_end > now())
    order by s.current_period_end desc nulls last
    limit 1
  ), prof as (
    select * from public.profiles p where p.id = (select auth.uid())
  )
  select
    (exists (select 1 from sub)
      or coalesce((select pro_trial_ends_at from prof) > now(), false)) as is_pro,
    coalesce(
      (select plan from sub),
      case when coalesce((select pro_trial_ends_at from prof) > now(), false) then 'trial' end
    ) as plan,
    coalesce(
      (select status from sub),
      case when coalesce((select pro_trial_ends_at from prof) > now(), false) then 'trialing' end
    ) as status,
    coalesce((select current_period_end from sub), (select pro_trial_ends_at from prof)) as current_period_end,
    coalesce((select cancel_at_period_end from sub), false) as cancel_at_period_end;
$$;
revoke all on function public.my_entitlement() from public, anon;
grant execute on function public.my_entitlement() to authenticated;
