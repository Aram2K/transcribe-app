-- Per-user daily cloud-transcription usage, to cap costs / trial abuse.
create table if not exists public.cloud_usage (
  user_id  uuid not null references auth.users(id) on delete cascade,
  day      date not null default (now() at time zone 'utc')::date,
  requests integer not null default 0,
  primary key (user_id, day)
);

alter table public.cloud_usage enable row level security;

drop policy if exists "read own cloud usage" on public.cloud_usage;
create policy "read own cloud usage" on public.cloud_usage
  for select using ((select auth.uid()) = user_id);

-- Atomically count one request and return whether the caller is still under the
-- daily cap. SECURITY DEFINER so it can write, but only ever the caller's row.
create or replace function public.use_cloud_quota(max_per_day integer)
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
  insert into public.cloud_usage (user_id, day, requests)
  values (uid, (now() at time zone 'utc')::date, 1)
  on conflict (user_id, day)
  do update set requests = public.cloud_usage.requests + 1
  returning requests into used;
  return used <= max_per_day;
end;
$$;

revoke all on function public.use_cloud_quota(integer) from public, anon;
grant execute on function public.use_cloud_quota(integer) to authenticated;
