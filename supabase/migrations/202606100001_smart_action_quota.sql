-- Smart Actions get their own daily counter, separate from cloud transcription.
-- use_cloud_quota() increments the shared `requests` column - if Smart Actions
-- used the same counter with a lower cap, heavy STT use would lock out Smart
-- Actions (and vice versa). A dedicated column keeps the two caps independent.
alter table public.cloud_usage
  add column if not exists action_requests integer not null default 0;

create or replace function public.use_smart_action_quota(max_per_day integer)
returns boolean
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  uid  uuid := (select auth.uid());
  used integer;
begin
  if uid is null then
    return false;
  end if;
  insert into public.cloud_usage (user_id, day, action_requests)
  values (uid, (now() at time zone 'utc')::date, 1)
  on conflict (user_id, day)
  do update set action_requests = public.cloud_usage.action_requests + 1
  returning action_requests into used;
  return used <= max_per_day;
end;
$$;

revoke all on function public.use_smart_action_quota(integer) from public, anon;
grant execute on function public.use_smart_action_quota(integer) to authenticated;
