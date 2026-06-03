-- Trial is now EXPLICIT: signup no longer auto-grants it; the user starts it.
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
begin
  insert into public.profiles (id, email, full_name)
  values (new.id, new.email, new.raw_user_meta_data->>'full_name')
  on conflict (id) do nothing;
  return new;
end;
$$;
revoke all on function public.handle_new_user() from public, anon, authenticated;

-- Explicit one-time trial start (3 days). The `pro_trial_started_at is null`
-- guard means it can only ever be started ONCE per account — re-calling never
-- resets or extends it (ungameable). Returns the trial end timestamp.
create or replace function public.start_pro_trial()
returns timestamptz
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_end timestamptz;
begin
  update public.profiles
     set pro_trial_started_at = now(),
         pro_trial_ends_at    = now() + interval '3 days'
   where id = (select auth.uid())
     and pro_trial_started_at is null
  returning pro_trial_ends_at into v_end;

  if v_end is null then
    select pro_trial_ends_at into v_end
      from public.profiles where id = (select auth.uid());
  end if;
  return v_end;
end;
$$;
revoke all on function public.start_pro_trial() from public, anon;
grant execute on function public.start_pro_trial() to authenticated;

-- my_entitlement gains `trial_available` (true if the trial was never started).
drop function if exists public.my_entitlement();
create function public.my_entitlement()
returns table (is_pro boolean, plan text, status text, current_period_end timestamptz, cancel_at_period_end boolean, trial_available boolean)
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
    coalesce((select cancel_at_period_end from sub), false) as cancel_at_period_end,
    ((select pro_trial_started_at from prof) is null) as trial_available;
$$;
revoke all on function public.my_entitlement() from public, anon;
grant execute on function public.my_entitlement() to authenticated;
