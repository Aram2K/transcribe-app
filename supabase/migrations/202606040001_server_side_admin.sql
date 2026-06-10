-- Admin is now a server-side, database-authoritative flag (no email in source).
alter table public.profiles add column if not exists is_admin boolean not null default false;

-- Grant the founder admin (set ONLY via SQL - never from the client).
update public.profiles set is_admin = true where lower(email) = 'aramatamian15@gmail.com';

-- is_pro() also honors admin so the founder always has full Pro (incl. cloud).
create or replace function public.is_pro()
returns boolean
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select
    exists (select 1 from public.profiles p
            where p.id = (select auth.uid()) and p.is_admin)
    or exists (
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

drop function if exists public.my_entitlement();
create function public.my_entitlement()
returns table (is_pro boolean, plan text, status text, current_period_end timestamptz, cancel_at_period_end boolean, trial_available boolean, is_admin boolean)
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  with sub as (
    select * from public.subscriptions s
    where s.user_id = (select auth.uid())
      and s.status in ('active', 'trialing')
      and (s.current_period_end is null or s.current_period_end > now())
    order by s.current_period_end desc nulls last
    limit 1
  ), prof as (
    select * from public.profiles p where p.id = (select auth.uid())
  )
  select
    (coalesce((select is_admin from prof), false)
      or exists (select 1 from sub)
      or coalesce((select pro_trial_ends_at from prof) > now(), false)) as is_pro,
    coalesce(
      (select plan from sub),
      case when coalesce((select pro_trial_ends_at from prof) > now(), false) then 'trial'
           when coalesce((select is_admin from prof), false) then 'admin' end
    ) as plan,
    coalesce(
      (select status from sub),
      case when coalesce((select pro_trial_ends_at from prof) > now(), false) then 'trialing' end
    ) as status,
    coalesce((select current_period_end from sub), (select pro_trial_ends_at from prof)) as current_period_end,
    coalesce((select cancel_at_period_end from sub), false) as cancel_at_period_end,
    ((select pro_trial_started_at from prof) is null) as trial_available,
    coalesce((select is_admin from prof), false) as is_admin;
$$;
revoke all on function public.my_entitlement() from public, anon;
grant execute on function public.my_entitlement() to authenticated;
