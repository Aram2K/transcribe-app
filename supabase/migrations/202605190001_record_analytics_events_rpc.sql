-- Public SECURITY DEFINER wrapper so the transcribe-analytics edge function
-- can insert into analytics.analytics_events without needing service_role
-- to be granted USAGE on the analytics schema (which would also require
-- adding `analytics` to PostgREST's exposed-schemas list).
--
-- The function trusts the caller (the edge function) to have already
-- filtered/sanitized events; it just persists what it's given. It is
-- written defensively to coalesce/truncate fields so a malformed item
-- never blocks the rest of the batch.

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
    (event, install_id, session_id, app_version, os, props, schema_version)
  select
    left(coalesce(elem->>'event', ''), 80),
    left(coalesce(elem->>'install_id', ''), 80),
    left(coalesce(elem->>'session_id', ''), 80),
    left(coalesce(elem->>'app_version', ''), 32),
    left(coalesce(elem->>'os', ''), 64),
    coalesce(elem->'props', '{}'::jsonb),
    coalesce(nullif(elem->>'schema_version', '')::int, 1)
  from jsonb_array_elements(events) as elem
  where coalesce(elem->>'event', '') <> ''
    and coalesce(elem->>'install_id', '') <> ''
    and coalesce(elem->>'session_id', '') <> '';

  get diagnostics inserted_count = row_count;
  return inserted_count;
end;
$$;

revoke all on function public.record_analytics_events(jsonb) from public;
grant execute on function public.record_analytics_events(jsonb)
  to anon, authenticated, service_role;
