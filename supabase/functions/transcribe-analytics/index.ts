// Anonymous usage analytics from the desktop app (telemetry.py).
//
// Deploy with verify_jwt=false: the app posts without a user token. Events are
// keyed by a random install id, never by account.

import 'jsr:@supabase/functions-js/edge-runtime.d.ts'
import { createClient } from 'npm:@supabase/supabase-js@2'

const corsHeaders = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Headers': 'authorization, x-client-info, apikey, content-type',
  'Access-Control-Allow-Methods': 'POST, OPTIONS',
  'Content-Type': 'application/json',
}

// App versions up to 1.9.0 post their whole queue (up to 200 events) in one
// request and discard it on success, so a lower cap would lose their events.
const MAX_EVENTS_PER_REQUEST = 200

// Keep in sync with ALLOWED_EVENTS in telemetry.py - events missing here are
// dropped (tests/test_telemetry.py fails when the two lists differ).
const allowedEvents = new Set([
  'app_started',
  'settings_opened',
  'settings_saved',
  'settings_tab_opened',
  'backend_selected',
  'privacy_mode_enabled',
  'privacy_mode_disabled',
  'history_opened',
  'history_exported',
  'history_cleared',
  'model_download_started',
  'model_download_completed',
  'model_download_failed',
  'model_removed',
  'gpu_download_started',
  'gpu_download_result',
  'gpu_offer_shown',
  'gpu_removed',
  'transcription_completed',
  'action_completed',
  'action_failed',
  'update_check_started',
  'update_check_result',
  'update_install_started',
  'update_install_result',
  'meeting_recording_started',
  'meeting_recording_failed',
  'meeting_notes_completed',
  'meeting_notes_failed',
  'meeting_exported',
  'paywall_viewed',
  'upgrade_clicked',
  'checkout_opened',
  'trial_started',
  'guest_trial_exhausted',
  'login_succeeded',
  'login_failed',
  'signup_verification_sent',
  'signed_out',
  'pro_activated',
  'onboarding_completed',
  'feedback_sent',
  'live_prompter_opened',
  'live_prompter_started',
  'live_prompter_suggestion',
  'file_transcription_started',
  'file_transcription_completed',
  'file_transcription_failed',
  'file_transcription_saved',
])

const sensitiveKeys = new Set([
  'text',
  'transcript',
  'transcription',
  'audio',
  'clipboard',
  'api_key',
  'google_api_key',
  'action_api_key',
  'authorization',
  'x_api_key',
  'token',
  'path',
  'file',
  'filename',
  'device',
  'device_name',
  'microphone',
  'window_title',
  'title',
  'question',
  'url',
  'email',
  'name',
  'full_name',
])

function asString(value: unknown, max = 120): string {
  if (typeof value !== 'string') return ''
  return value.slice(0, max)
}

// A value that quotes an email address is dropped whatever its key (older
// clients sent raw sign-in error text, which can echo the address back).
const emailPattern = /[^\s@"'<>]+@[^\s@"'<>]+\.[a-z]{2,}/i

function sanitizeProps(raw: unknown): Record<string, unknown> {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return {}
  const clean: Record<string, unknown> = {}
  for (const [key, value] of Object.entries(raw as Record<string, unknown>)) {
    const safeKey = key.toLowerCase().slice(0, 64)
    if (!safeKey || sensitiveKeys.has(safeKey)) continue
    if (typeof value === 'boolean' || value === null) {
      clean[safeKey] = value
    } else if (typeof value === 'number' && Number.isFinite(value)) {
      clean[safeKey] = value
    } else if (typeof value === 'string') {
      if (emailPattern.test(value)) continue
      clean[safeKey] = value.slice(0, 80)
    }
  }
  return clean
}

Deno.serve(async (req: Request) => {
  if (req.method === 'OPTIONS') {
    return new Response(JSON.stringify({ ok: true }), { headers: corsHeaders })
  }
  if (req.method !== 'POST') {
    return new Response(JSON.stringify({ error: 'method_not_allowed' }), { status: 405, headers: corsHeaders })
  }

  let payload: unknown
  try {
    payload = await req.json()
  } catch (_error) {
    return new Response(JSON.stringify({ error: 'invalid_json' }), { status: 400, headers: corsHeaders })
  }

  const items = Array.isArray((payload as { events?: unknown })?.events)
    ? ((payload as { events: unknown[] }).events).slice(0, MAX_EVENTS_PER_REQUEST)
    : []

  const rows = items.flatMap((item) => {
    if (!item || typeof item !== 'object') return []
    const event = asString((item as { event?: unknown }).event, 80)
    if (!allowedEvents.has(event)) return []
    const installId = asString((item as { install_id?: unknown }).install_id, 80)
    const sessionId = asString((item as { session_id?: unknown }).session_id, 80)
    if (!installId || !sessionId) return []
    // When the event happened on the user's machine (unix seconds). The RPC
    // falls back to the arrival time when it's missing or implausible.
    const occurredAt = Number((item as { timestamp?: unknown }).timestamp)
    return [{
      event,
      install_id: installId,
      session_id: sessionId,
      event_id: asString((item as { event_id?: unknown }).event_id, 64) || null,
      occurred_at: Number.isFinite(occurredAt) ? occurredAt : null,
      app_version: asString((item as { app_version?: unknown }).app_version, 32),
      os: asString((item as { os?: unknown }).os, 64),
      props: sanitizeProps((item as { props?: unknown }).props),
      schema_version: Math.trunc(Number((item as { schema?: unknown }).schema)) || 1,
    }]
  })

  if (!rows.length) {
    return new Response(JSON.stringify({ inserted: 0 }), { headers: corsHeaders })
  }

  const supabaseUrl = Deno.env.get('SUPABASE_URL')
  const serviceRoleKey = Deno.env.get('SUPABASE_SERVICE_ROLE_KEY')
  if (!supabaseUrl || !serviceRoleKey) {
    return new Response(JSON.stringify({ error: 'server_not_configured' }), { status: 500, headers: corsHeaders })
  }

  const supabase = createClient(supabaseUrl, serviceRoleKey, { auth: { persistSession: false } })
  // We call a SECURITY DEFINER RPC in the public schema instead of writing
  // to `analytics.analytics_events` directly. That avoids needing the
  // `analytics` schema to be added to PostgREST's exposed-schemas list
  // and avoids granting service_role rights on the analytics schema.
  // Only service_role may execute it (see the analytics pipeline migration).
  const { data, error } = await supabase.rpc('record_analytics_events', { events: rows })
  if (error) {
    console.error('analytics insert failed', error)
    return new Response(JSON.stringify({ error: 'insert_failed' }), { status: 500, headers: corsHeaders })
  }

  return new Response(JSON.stringify({ inserted: data ?? rows.length }), { headers: corsHeaders })
})
