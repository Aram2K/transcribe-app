// Managed cloud transcription proxy - the Pro moat.
//
// Flow: verify the caller's Supabase JWT → confirm is_pro() server-side (paid
// or trial) → enforce a daily quota → call Google Speech-to-Text with the
// SERVER-held key → return the transcript. The Google key never reaches the
// client, so Pro cloud transcription cannot be used without a valid account +
// entitlement, even though the app is open source.
//
// Secret required: GOOGLE_STT_KEY (the rotated Google Cloud Speech key).
// Auto-provided by Supabase: SUPABASE_URL, SUPABASE_ANON_KEY.
// Deploy with verify_jwt=false; we validate the token via auth.getUser().

import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL") ?? "";
const ANON_KEY = Deno.env.get("SUPABASE_ANON_KEY") ?? "";
const GOOGLE_KEY = Deno.env.get("GOOGLE_STT_KEY") ?? "";
const DAILY_CAP = 3000; // requests/user/day

const cors = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

function json(obj: unknown, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { ...cors, "Content-Type": "application/json" },
  });
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors });
  if (req.method !== "POST") return json({ error: "method_not_allowed" }, 405);

  const authHeader = req.headers.get("Authorization") ?? "";
  if (!authHeader.startsWith("Bearer ")) return json({ error: "unauthorized" }, 401);

  // Scope a client to the caller's JWT so RLS + auth.uid() apply.
  const supa = createClient(SUPABASE_URL, ANON_KEY, {
    global: { headers: { Authorization: authHeader } },
  });

  const { data: userData, error: userErr } = await supa.auth.getUser();
  if (userErr || !userData?.user) return json({ error: "unauthorized" }, 401);

  // Entitlement: paid subscription OR active trial (server-side truth).
  const { data: pro, error: proErr } = await supa.rpc("is_pro");
  if (proErr) return json({ error: "entitlement_check_failed" }, 500);
  if (pro !== true) return json({ error: "pro_required" }, 403);

  // Daily quota.
  const { data: underCap, error: qErr } = await supa.rpc("use_cloud_quota", { max_per_day: DAILY_CAP });
  if (qErr) return json({ error: "quota_check_failed" }, 500);
  if (underCap !== true) return json({ error: "quota_exceeded" }, 429);

  let body: any;
  try { body = await req.json(); } catch { return json({ error: "bad_request" }, 400); }
  const audio = body?.audio;
  const sampleRate = body?.sample_rate ?? 16000;
  const language = body?.language ?? "auto";
  if (!audio || typeof audio !== "string") return json({ error: "no_audio" }, 400);

  const langCode = language === "hy" ? "hy-AM" : language === "ru" ? "ru-RU" : "en-US";
  const payload: any = {
    config: { encoding: "LINEAR16", sampleRateHertz: sampleRate, languageCode: langCode },
    audio: { content: audio },
  };
  if (language === "auto") {
    payload.config.alternativeLanguageCodes = ["hy-AM", "en-US", "ru-RU", "fr-FR", "de-DE", "es-ES", "ar-EG"];
  }

  const gResp = await fetch(
    `https://speech.googleapis.com/v1/speech:recognize?key=${GOOGLE_KEY}`,
    { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) },
  );
  if (!gResp.ok) {
    const t = await gResp.text();
    return json({ error: "stt_failed", detail: t.slice(0, 160) }, 502);
  }
  const gData = await gResp.json();
  const results = gData.results ?? [];
  const text = results.map((r: any) => r.alternatives?.[0]?.transcript ?? "").join(" ").trim();
  const detected = (results[0]?.languageCode ?? "en-US").split("-")[0];
  return json({ text, lang: detected });
});
