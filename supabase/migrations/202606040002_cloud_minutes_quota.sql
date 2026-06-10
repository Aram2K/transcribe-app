-- Cost-accurate metering: track audio SECONDS per user per month (cost is
-- per-minute, not per-request). The monthly cap guarantees a margin floor.
alter table public.cloud_usage add column if not exists seconds_used numeric not null default 0;

create or replace function public.use_cloud_seconds(add_seconds numeric, max_seconds integer)
returns boolean
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  uid     uuid := (select auth.uid());
  mbucket date := date_trunc('month', now() at time zone 'utc')::date;  -- one row/user/month
  used    numeric;
begin
  if uid is null then
    return false;
  end if;
  insert into public.cloud_usage (user_id, day, seconds_used)
  values (uid, mbucket, greatest(coalesce(add_seconds, 0), 0))
  on conflict (user_id, day)
  do update set seconds_used = public.cloud_usage.seconds_used + greatest(coalesce(add_seconds, 0), 0)
  returning seconds_used into used;
  return used <= max_seconds;
end;
$$;
revoke all on function public.use_cloud_seconds(numeric, integer) from public, anon;
grant execute on function public.use_cloud_seconds(numeric, integer) to authenticated;
