// In-app feedback -> Supabase table (+ optional GitHub issue).
//
// A signed-in user types feedback / a feature request, optionally pasting a
// screenshot. We verify their Supabase JWT, upload any image to the public
// `feedback` storage bucket, and ALWAYS save the feedback to the `feedback`
// table (service role) so "Send" works out of the box. If a GitHub token is
// configured we also open an issue, which sends the founder GitHub's normal
// "new issue" email - no separate mail service needed.
//
// Optional secrets (the table save works without them):
//   GITHUB_TOKEN - fine-grained PAT with "Issues: read and write" on the repo.
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

  const svc = SERVICE_KEY ? createClient(SUPABASE_URL, SERVICE_KEY) : null;
  if (!svc) return json({ error: "server_misconfigured" }, 500);

  // Optional screenshots -> public storage URLs. Accepts a single `image` or an
  // `images` array (up to 4).
  const imageList: string[] = Array.isArray(body?.images)
    ? body.images
    : (body?.image ? [body.image] : []);
  const imageUrls: string[] = [];
  for (const item of imageList.slice(0, 4)) {
    try {
      const s = (item ?? "").toString();
      const raw = s.includes(",") ? s.split(",", 2)[1] : s;
      const bytes = b64ToBytes(raw);
      if (bytes.length > 0 && bytes.length <= 6_000_000) {
        const path = `${user.id}/${Date.now()}-${imageUrls.length}.png`;
        const up = await svc.storage.from("feedback").upload(path, bytes, {
          contentType: "image/png",
          upsert: false,
        });
        if (!up.error) {
          const { data: pub } = svc.storage.from("feedback").getPublicUrl(path);
          if (pub?.publicUrl) imageUrls.push(pub.publicUrl);
        }
      }
    } catch (_e) {
      // Non-fatal: still save the feedback without this image.
    }
  }
  const imageUrl = imageUrls.length ? imageUrls.join("\n") : "";

  // Always persist the feedback so "Send" works even before a GitHub token is
  // set. The founder can read it in the Supabase dashboard meanwhile.
  const { data: row, error: insErr } = await svc.from("feedback").insert({
    user_id: user.id,
    email: user.email ?? null,
    message,
    image_url: imageUrl || null,
    app_version: appVersion || null,
    category,
  }).select("id").single();
  if (insErr) return json({ error: "save_failed", detail: (insErr.message ?? "").slice(0, 120) }, 500);

  // Bonus: open a GitHub issue (which emails the founder) when a token is set.
  let issueUrl: string | null = null;
  if (GITHUB_TOKEN) {
    try {
      const imageMd = imageUrls.map((u) => `\n\n![screenshot](${u})`).join("");
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
        body: JSON.stringify({ title, body: issueBody }),
      });
      if (ghResp.ok) {
        const issue = await ghResp.json();
        issueUrl = issue.html_url ?? null;
        if (row?.id) {
          await svc.from("feedback").update({ issue_url: issueUrl, github_error: null }).eq("id", row.id);
        }
      } else {
        const detail = (await ghResp.text()).slice(0, 250);
        if (row?.id) {
          await svc.from("feedback").update({ github_error: `HTTP ${ghResp.status}: ${detail}` }).eq("id", row.id);
        }
      }
    } catch (e) {
      if (row?.id) {
        await svc.from("feedback").update({ github_error: `exception: ${String(e).slice(0, 180)}` }).eq("id", row.id);
      }
    }
  }

  return json({ ok: true, saved: true, url: issueUrl });
});
