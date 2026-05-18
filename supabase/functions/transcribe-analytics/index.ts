import 'jsr:@supabase/functions-js/edge-runtime.d.ts'
import { createClient } from 'npm:@supabase/supabase-js@2'

const corsHeaders = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Headers': 'authorization, x-client-info, apikey, content-type',
  'Access-Control-Allow-Methods': 'POST, OPTIONS',
  'Content-Type': 'application/json',
}

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
  'transcription_completed',
  'action_completed',
  'action_failed',
  'update_check_started',
  'update_check_result',
  'update_install_started',
  'update_install_result',
])

const sensitiveKeys = new Set([
  'text',
  'transcript',
  'transcription',
  'audio',
  'clipboard',
  'api_key',
  'google_api_key',
  'path',
  'file',
  'filename',
  'device',
  'device_name',
  'microphone',
  'window_title',
])

function asString(value: unknown, max = 120): string {
  if (typeof value !== 'string') return ''
  return value.slice(0, max)
}

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
    ? ((payload as { events: unknown[] }).events).slice(0, 50)
    : []

  const rows = items.flatMap((item) => {
    if (!item || typeof item !== 'object') return []
    const event = asString((item as { event?: unknown }).event, 80)
    if (!allowedEvents.has(event)) return []
    const installId = asString((item as { install_id?: unknown }).install_id, 80)
    const sessionId = asString((item as { session_id?: unknown }).session_id, 80)
    if (!installId || !sessionId) return []
    return [{
      event,
      install_id: installId,
      session_id: sessionId,
      app_version: asString((item as { app_version?: unknown }).app_version, 32),
      os: asString((item as { os?: unknown }).os, 64),
      props: sanitizeProps((item as { props?: unknown }).props),
      schema_version: Number((item as { schema?: unknown }).schema) || 1,
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
  const { error } = await supabase.schema('analytics').from('analytics_events').insert(rows)
  if (error) {
    console.error('analytics insert failed', error)
    return new Response(JSON.stringify({ error: 'insert_failed' }), { status: 500, headers: corsHeaders })
  }

  return new Response(JSON.stringify({ inserted: rows.length }), { headers: corsHeaders })
})
