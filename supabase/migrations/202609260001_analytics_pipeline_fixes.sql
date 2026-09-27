-- Analytics pipeline fixes.
--
-- 1. record_analytics_events() was executable by anon/authenticated, so anyone
--    holding the (public) publishable key could call it through PostgREST,
--    skipping the edge function's allowlist and sanitizer, and insert an
--    unlimited number of rows. Only the transcribe-analytics edge function
--    (service_role) may call it now.
-- 2. occurred_at: when the event happened on the user's machine. Events queued
--    while offline (e.g. while the project was paused) used to be dated the day
--    they were uploaded; daily totals now count them on the day they happened.
-- 3. event_id: uploads are idempotent, so a batch the app retries after a lost
--    response is not counted twice.

alter table analytics.analytics_events
  add column if not exists occurred_at timestamptz,
  add column if not exists event_id text;

update analytics.analytics_events
   set occurred_at = received_at
 where occurred_at is null;

alter table analytics.analytics_events
  alter column occurred_at set default now(),
  alter column occurred_at set not null;

create unique index if not exists analytics_events_event_id_key
  on analytics.analytics_events (event_id)
  where event_id is not null;

create index if not exists analytics_events_occurred_at_idx
  on analytics.analytics_events (occurred_at desc);

create or replace function analytics.rollup_analytics_event()
returns trigger
language plpgsql
security definer
set search_path = analytics, pg_temp
as $$
begin
  insert into analytics.analytics_daily (day, event, app_version, count)
  values (coalesce(new.occurred_at, new.received_at)::date, new.event,
          coalesce(new.app_version, ''), 1)
  on conflict (day, event, app_version)
  do update set count = analytics.analytics_daily.count + 1;
  return new;
end;
$$;

create or replace function public.record_analytics_events(events jsonb)
returns integer
language plpgsql
security definer
set search_path = analytics, public, pg_temp
as $$
declare
  inserted_count integer := 0;
begin
  if events is null or jsonb_typeof(events) <> 'array' then
    return 0;
  end if;

  insert into analytics.analytics_events
    (event, install_id, session_id, app_version, os, props, schema_version,
     occurred_at, event_id)
  select
    left(coalesce(elem->>'event', ''), 80),
    left(coalesce(elem->>'install_id', ''), 80),
    left(coalesce(elem->>'session_id', ''), 80),
    left(coalesce(elem->>'app_version', ''), 32),
    left(coalesce(elem->>'os', ''), 64),
    case when jsonb_typeof(elem->'props') = 'object'
         then elem->'props' else '{}'::jsonb end,
    case when jsonb_typeof(elem->'schema_version') = 'number'
         then least(greatest((elem->>'schema_version')::numeric, 1), 1000)::int
         else 1 end,
    -- The client clock is trusted only within a plausible window; otherwise
    -- the event is dated when it arrived.
    case when t.ts between now() - interval '60 days' and now() + interval '10 minutes'
         then t.ts else now() end,
    nullif(left(coalesce(elem->>'event_id', ''), 64), '')
  from jsonb_array_elements(events) as elem
  cross join lateral (
    select case when jsonb_typeof(elem->'occurred_at') = 'number' then
             case when (elem->>'occurred_at')::double precision between 0 and 32503680000
                  then to_timestamp((elem->>'occurred_at')::double precision) end
           end as ts
  ) t
  where coalesce(elem->>'event', '') <> ''
    and coalesce(elem->>'install_id', '') <> ''
    and coalesce(elem->>'session_id', '') <> ''
  on conflict (event_id) where event_id is not null do nothing;

  get diagnostics inserted_count = row_count;
  return inserted_count;
end;
$$;

revoke all on function public.record_analytics_events(jsonb) from public, anon, authenticated;
grant execute on function public.record_analytics_events(jsonb) to service_role;
