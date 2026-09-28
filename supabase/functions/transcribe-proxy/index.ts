// Managed cloud transcription proxy - the Pro moat.
//
// Flow: verify the caller's Supabase JWT -> confirm is_pro() server-side (paid
// or trial) -> enforce a daily quota -> transcribe with a SERVER-held key ->
// return the transcript. Keys never reach the client, so Pro cloud transcription
// cannot be used without a valid account + entitlement, even though the app is
// open source.
//
// Provider routing: the client may request `provider: "gemini" | "mistral"`.
// We use whichever key is actually configured (honoring the request when its
// key exists), so STT works with EITHER GOOGLE_STT_KEY or MISTRAL_STT_KEY set.
//
// Secrets: GOOGLE_STT_KEY or GEMINI_KEY (Google AI Studio / Gemini key) and/or
//          MISTRAL_STT_KEY. At least one must be set or the proxy returns 503.
// Auto-provided by Supabase: SUPABASE_URL, SUPABASE_ANON_KEY.
// Deploy with verify_jwt=false; we validate the token via auth.getUser().

import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL") ?? "";
const ANON_KEY = Deno.env.get("SUPABASE_ANON_KEY") ?? "";
const GOOGLE_KEY = Deno.env.get("GOOGLE_STT_KEY") || Deno.env.get("GEMINI_KEY") || ""; // either name works
const MISTRAL_KEY = Deno.env.get("MISTRAL_STT_KEY") ?? "";
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

function b64ToBytes(b64: string): Uint8Array {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

const LANG_NAMES: Record<string, string> = {
  hy: "Armenian", ru: "Russian", en: "English", fr: "French",
  de: "German", es: "Spanish", ar: "Arabic",
};
// Languages Mistral Voxtral handles well (ISO-639-1). Others are auto-detected.
const MISTRAL_LANGS = new Set(["en", "es", "fr", "de", "it", "nl", "pt", "hi", "ar", "ru"]);

// Non-Latin scripts; the rest are written in Latin letters.
const SCRIPT: Record<string, string> = { hy: "the Armenian alphabet", ru: "Cyrillic", ar: "Arabic script" };

function joinNames(items: string[]): string {
  return items.length <= 1 ? items.join("") : items.slice(0, -1).join(", ") + " and " + items[items.length - 1];
}

// The language instruction for clients that send `languages` (v1.9.3+) - the
// same wording as speech_langs.cloud_hint in the app. Foreign words are kept
// as spoken, and "multi" (Mixed languages) lists the languages to expect
// (any language when fewer than two are picked), switching anywhere, even
// mid-sentence.
function languageHint(language: string, languages: string[]): string {
  const nm = LANG_NAMES[language];
  if (nm) {
    const how = SCRIPT[language] ? ` in ${SCRIPT[language]}, never transliterated into Latin letters` : "";
    return ` The speaker speaks mainly ${nm}. Write ${nm}${how}. If they use words or phrases ` +
      `from another language, keep those exactly as spoken, in that language's own script - ` +
      `don't translate them.`;
  }
  if (language === "multi") {
    const mix = languages.filter((c) => LANG_NAMES[c]);
    if (mix.length < 2) {
      return " The speaker may switch languages, even in the middle of a sentence. " +
        "Write every word in the language it was spoken in, in that language's " +
        "own script. Never translate, and never turn the whole text into one " +
        "language.";
    }
    const latin = mix.filter((c) => !SCRIPT[c]).map((c) => LANG_NAMES[c]);
    const bits = mix.filter((c) => SCRIPT[c]).map((c) => `${LANG_NAMES[c]} in ${SCRIPT[c]}`);
    if (latin.length) bits.push(`${joinNames(latin)} in Latin letters`);
    return ` The speaker switches between ${joinNames(mix.map((c) => LANG_NAMES[c]))}, often in ` +
      `the middle of a sentence. Write every word in the language it was spoken in, in that ` +
      `language's own script (${bits.join(", ")}). Never translate, and never turn the whole ` +
      `text into one language.`;
  }
  return " If the speaker switches languages, keep every word in the language it was spoken " +
    "in, in its own script - don't translate.";
}

async function transcribeGemini(audioB64: string, language: string, languages: string[] | null) {
  const nm = LANG_NAMES[language];
  // Older clients (no `languages`) keep the exact old prompt.
  const hint = languages !== null
    ? languageHint(language, languages)
    : nm
    ? ` The speaker is speaking ${nm}. Transcribe in ${nm} using its native script and return only ${nm} text.`
    : "";
  const payload = {
    contents: [{
      parts: [
        { text: "Transcribe this audio verbatim. Output only the exact spoken words " +
                "with correct punctuation and capitalization, and nothing else." + hint },
        { inline_data: { mime_type: "audio/wav", data: audioB64 } },
      ],
    }],
    generationConfig: { temperature: 0 },
  };
  const r = await fetch(
    `https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=${GOOGLE_KEY}`,
    { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) },
  );
  if (!r.ok) return { ok: false, detail: (await r.text()).slice(0, 160) };
  const d = await r.json();
  const parts = d.candidates?.[0]?.content?.parts ?? [];
  return { ok: true, text: parts.map((p: any) => p.text ?? "").join("").trim() };
}

async function transcribeMistral(audioB64: string, language: string) {
  const form = new FormData();
  form.append("model", "voxtral-mini-latest");
  form.append("file", new Blob([b64ToBytes(audioB64)], { type: "audio/wav" }), "audio.wav");
  if (MISTRAL_LANGS.has(language)) form.append("language", language);
  const r = await fetch("https://api.mistral.ai/v1/audio/transcriptions", {
    method: "POST",
    headers: { "Authorization": `Bearer ${MISTRAL_KEY}` },
    body: form,
  });
  if (!r.ok) return { ok: false, detail: (await r.text()).slice(0, 160) };
  const d = await r.json();
  return { ok: true, text: (d.text ?? "").trim() };
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors });
  if (req.method !== "POST") return json({ error: "method_not_allowed" }, 405);

  const authHeader = req.headers.get("Authorization") ?? "";
  if (!authHeader.startsWith("Bearer ")) return json({ error: "unauthorized" }, 401);

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
  const languages: string[] | null = Array.isArray(body?.languages)
    ? body.languages.filter((c: unknown) => typeof c === "string").slice(0, 8)
    : null;
  const provider = (body?.provider ?? "gemini").toString();
  if (!audio || typeof audio !== "string") return json({ error: "no_audio" }, 400);

  // Use whichever provider actually has a key. Honor the client's choice when
  // its key exists, otherwise fall through to the one that IS configured - so
  // managed STT works as long as EITHER GOOGLE_STT_KEY or MISTRAL_STT_KEY is
  // set (the default "gemini" choice no longer hard-requires GOOGLE_STT_KEY,
  // which is the bug that made cloud transcription 502 when only the Mistral
  // key was set for Smart Actions).
  const order = provider === "mistral" ? ["mistral", "gemini"] : ["gemini", "mistral"];
  const available = order.filter((p) =>
    (p === "gemini" && GOOGLE_KEY) || (p === "mistral" && MISTRAL_KEY));
  if (available.length === 0) {
    return json({ error: "stt_not_configured" }, 503);
  }
  let result: any = { ok: false, detail: "no provider" };
  for (const p of available) {
    result = p === "mistral"
      ? await transcribeMistral(audio, language)
      : await transcribeGemini(audio, language, languages);
    if (result.ok) break;
  }
  if (!result.ok) return json({ error: "stt_failed", detail: result.detail }, 502);

  const detected = language && language !== "auto" ? language : "en";
  return json({ text: result.text, lang: detected });
});
