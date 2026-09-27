-- Keep raw analytics events for 365 days so the table can't grow forever.
-- analytics.analytics_daily (per-day totals) is never pruned, so long-term
-- trends survive. Change the interval below to keep raw events longer.

create extension if not exists pg_cron with schema pg_catalog;

select cron.schedule(
  'analytics-events-retention',
  '17 3 * * *',  -- daily at 03:17 UTC
  $$delete from analytics.analytics_events where occurred_at < now() - interval '365 days'$$
);
