// Managed cloud transcription proxy - the Pro moat.
//
// Flow: verify the caller's Supabase JWT → confirm is_pro() server-side (paid
// or trial) → enforce a daily quota → transcribe with Google Gemini using the
// SERVER-held key → return the transcript. The key never reaches the client, so
// Pro cloud transcription cannot be used without a valid account + entitlement,
// even though the app is open source.
//
// We use the Gemini (AI Studio) API because it accepts a plain API key; Google
// Cloud Speech-to-Text rejects API keys (it requires OAuth/service accounts).
// The client sends a full base64 WAV (with header) in `audio`.
//
// Secret required: GOOGLE_STT_KEY (a Google AI Studio / Gemini API key).
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
  const language = body?.language ?? "auto";
  if (!audio || typeof audio !== "string") return json({ error: "no_audio" }, 400);

  const langNames: Record<string, string> = {
    hy: "Armenian", ru: "Russian", en: "English", fr: "French",
    de: "German", es: "Spanish", ar: "Arabic",
  };
  const langHint = langNames[language] ? ` The audio is spoken in ${langNames[language]}.` : "";
  const model = "gemini-2.5-flash";
  const payload = {
    contents: [{
      parts: [
        { text: "Transcribe this audio verbatim. Output only the exact spoken words " +
                "with correct punctuation and capitalization, and nothing else." + langHint },
        { inline_data: { mime_type: "audio/wav", data: audio } },
      ],
    }],
    generationConfig: { temperature: 0 },
  };

  const gResp = await fetch(
    `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent?key=${GOOGLE_KEY}`,
    { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) },
  );
  if (!gResp.ok) {
    const t = await gResp.text();
    return json({ error: "stt_failed", detail: t.slice(0, 160) }, 502);
  }
  const gData = await gResp.json();
  const parts = gData.candidates?.[0]?.content?.parts ?? [];
  const text = parts.map((p: any) => p.text ?? "").join("").trim();
  const detected = language && language !== "auto" ? language : "en";
  return json({ text, lang: detected });
});
