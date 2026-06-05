// In-app feedback -> GitHub issue.
//
// A signed-in user types feedback / a feature request (optionally with a pasted
// or attached screenshot). We verify their Supabase JWT, upload any image to the
// public `feedback` storage bucket with the service role, then open a GitHub
// issue via the REST API using a SERVER-held token. The founder receives
// GitHub's normal "new issue" notification email, so no separate mail service is
// needed.
//
// Secrets required:
//   GITHUB_TOKEN - a fine-grained PAT with "Issues: read and write" on the repo.
//   GITHUB_REPO  - "owner/repo" (defaults to Aram2K/transcribe-app).
// Auto-provided by Supabase: SUPABASE_URL, SUPABASE_ANON_KEY, SUPABASE_SERVICE_ROLE_KEY.
// Deploy with verify_jwt=false; we validate the token via auth.getUser().

import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL") ?? "";
const ANON_KEY = Deno.env.get("SUPABASE_ANON_KEY") ?? "";
const SERVICE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";
const GITHUB_TOKEN = Deno.env.get("GITHUB_TOKEN") ?? "";
const GITHUB_REPO = Deno.env.get("GITHUB_REPO") ?? "Aram2K/transcribe-app";

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
  const user = userData.user;

  let body: any;
  try { body = await req.json(); } catch { return json({ error: "bad_request" }, 400); }

  const message = (body?.message ?? "").toString().trim();
  if (!message) return json({ error: "empty_message" }, 400);
  if (message.length > 5000) return json({ error: "message_too_long" }, 400);
  const appVersion = (body?.app_version ?? "").toString().slice(0, 20);
  const category = (body?.category ?? "feedback").toString().slice(0, 30);

  if (!GITHUB_TOKEN) return json({ error: "feedback_not_configured" }, 503);

  // Optional screenshot -> public storage URL.
  let imageMd = "";
  const imageB64 = (body?.image ?? "").toString();
  if (imageB64 && SERVICE_KEY) {
    try {
      const raw = imageB64.includes(",") ? imageB64.split(",", 2)[1] : imageB64;
      const bytes = b64ToBytes(raw);
      if (bytes.length > 0 && bytes.length <= 6_000_000) {
        const svc = createClient(SUPABASE_URL, SERVICE_KEY);
        const path = `${user.id}/${Date.now()}.png`;
        const up = await svc.storage.from("feedback").upload(path, bytes, {
          contentType: "image/png",
          upsert: false,
        });
        if (!up.error) {
          const { data: pub } = svc.storage.from("feedback").getPublicUrl(path);
          if (pub?.publicUrl) imageMd = `\n\n![screenshot](${pub.publicUrl})`;
        }
      }
    } catch (_e) {
      // Non-fatal: still file the issue without the image.
    }
  }

  const firstLine = message.split("\n")[0].slice(0, 70).trim() || "feedback";
  const title = `[${category}] ${firstLine}`;
  const issueBody =
    `${message}${imageMd}\n\n---\n` +
    `Submitted from Transcribe ${appVersion || "(unknown version)"}\n` +
    `From: ${user.email ?? "unknown"} (user ${user.id})`;

  const ghResp = await fetch(`https://api.github.com/repos/${GITHUB_REPO}/issues`, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${GITHUB_TOKEN}`,
      "Accept": "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "transcribe-feedback",
      "Content-Type": "application/json",
    },
    // No `labels`: GitHub 422s if a label doesn't already exist in the repo.
    // The "[category]" title prefix categorizes the issue instead.
    body: JSON.stringify({ title, body: issueBody }),
  });

  if (!ghResp.ok) {
    const t = await ghResp.text();
    return json({ error: "github_failed", detail: t.slice(0, 160) }, 502);
  }
  const issue = await ghResp.json();
  return json({ ok: true, url: issue.html_url });
});
