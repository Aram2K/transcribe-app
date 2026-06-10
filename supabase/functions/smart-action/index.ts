// Managed Smart Actions proxy - the Pro AI brain.
//
// Pro users run Smart Actions (translate / rewrite / summarize / email / meeting
// notes) through the SERVER-held Mistral key, so they never need their own API
// key. Flow: verify JWT -> is_pro() server-side -> daily quota -> Mistral chat
// with the messages the client built -> return the text.
//
// Secret required: MISTRAL_STT_KEY (the founder's Mistral key; used for chat too).
// Auto-provided: SUPABASE_URL, SUPABASE_ANON_KEY. Deploy with verify_jwt=false.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL") ?? "";
const ANON_KEY = Deno.env.get("SUPABASE_ANON_KEY") ?? "";
const MISTRAL_KEY = Deno.env.get("MISTRAL_STT_KEY") ?? "";
const MODEL = "mistral-small-latest"; // good-enough, fast + cheap
// 200/day is ~10x any human's realistic dictation pace - a cost-abuse ceiling,
// not a usage limit. Counted separately from the cloud-STT quota.
const DAILY_CAP = 200;

const cors = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

function json(obj: unknown, status = 200) {
  return new Response(JSON.stringify(obj), { status, headers: { ...cors, "Content-Type": "application/json" } });
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors });
  if (req.method !== "POST") return json({ error: "method_not_allowed" }, 405);

  const authHeader = req.headers.get("Authorization") ?? "";
  if (!authHeader.startsWith("Bearer ")) return json({ error: "unauthorized" }, 401);

  const supa = createClient(SUPABASE_URL, ANON_KEY, { global: { headers: { Authorization: authHeader } } });
  const { data: userData, error: userErr } = await supa.auth.getUser();
  if (userErr || !userData?.user) return json({ error: "unauthorized" }, 401);

  const { data: pro, error: proErr } = await supa.rpc("is_pro");
  if (proErr) return json({ error: "entitlement_check_failed" }, 500);
  if (pro !== true) return json({ error: "pro_required" }, 403);

  const { data: underCap, error: qErr } = await supa.rpc("use_smart_action_quota", { max_per_day: DAILY_CAP });
  if (qErr) return json({ error: "quota_check_failed" }, 500);
  if (underCap !== true) return json({ error: "quota_exceeded" }, 429);

  let body: any;
  try { body = await req.json(); } catch { return json({ error: "bad_request" }, 400); }
  const messages = body?.messages;
  const maxTokens = Math.min(Number(body?.max_tokens) || 600, 2000);
  if (!Array.isArray(messages) || messages.length === 0) return json({ error: "no_messages" }, 400);

  if (!MISTRAL_KEY) return json({ error: "actions_not_configured" }, 503);

  const r = await fetch("https://api.mistral.ai/v1/chat/completions", {
    method: "POST",
    headers: { "Authorization": `Bearer ${MISTRAL_KEY}`, "Content-Type": "application/json" },
    body: JSON.stringify({ model: MODEL, messages, temperature: 0.1, max_tokens: maxTokens }),
  });
  if (!r.ok) {
    const t = await r.text();
    return json({ error: "action_failed", detail: t.slice(0, 160) }, 502);
  }
  const d = await r.json();
  const text = (d.choices?.[0]?.message?.content ?? "").trim();
  return json({ text });
});
