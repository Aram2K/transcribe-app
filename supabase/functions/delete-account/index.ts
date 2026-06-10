// GDPR account deletion - the user's right to erasure, self-served.
//
// Flow: verify JWT -> cancel any active Stripe subscriptions (if a customer id
// and STRIPE_SECRET_KEY exist) -> scrub PII from feedback rows -> delete the
// auth user with the service role. profiles + cloud_usage cascade via FK;
// feedback.user_id is set null and we blank its email column ourselves.
//
// Secrets: SUPABASE_SERVICE_ROLE_KEY (auto), STRIPE_SECRET_KEY (optional - if
// missing, the account is still deleted; any live subscription must then be
// cancelled manually from the Stripe dashboard).

import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL") ?? "";
const ANON_KEY = Deno.env.get("SUPABASE_ANON_KEY") ?? "";
const SERVICE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";
const STRIPE_KEY = Deno.env.get("STRIPE_SECRET_KEY") ?? "";

const cors = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

function json(obj: unknown, status = 200) {
  return new Response(JSON.stringify(obj), { status, headers: { ...cors, "Content-Type": "application/json" } });
}

async function cancelStripeSubscriptions(customerId: string): Promise<boolean> {
  if (!STRIPE_KEY || !customerId) return false;
  try {
    const list = await fetch(
      `https://api.stripe.com/v1/subscriptions?customer=${encodeURIComponent(customerId)}&status=all&limit=20`,
      { headers: { Authorization: `Bearer ${STRIPE_KEY}` } },
    );
    if (!list.ok) return false;
    const data = await list.json();
    for (const sub of data?.data ?? []) {
      if (["active", "trialing", "past_due", "unpaid"].includes(sub.status)) {
        await fetch(`https://api.stripe.com/v1/subscriptions/${sub.id}`, {
          method: "DELETE",
          headers: { Authorization: `Bearer ${STRIPE_KEY}` },
        });
      }
    }
    return true;
  } catch (_e) {
    return false;
  }
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors });
  if (req.method !== "POST") return json({ error: "method_not_allowed" }, 405);

  const authHeader = req.headers.get("Authorization") ?? "";
  if (!authHeader.startsWith("Bearer ")) return json({ error: "unauthorized" }, 401);

  const supa = createClient(SUPABASE_URL, ANON_KEY, { global: { headers: { Authorization: authHeader } } });
  const { data: userData, error: userErr } = await supa.auth.getUser();
  if (userErr || !userData?.user) return json({ error: "unauthorized" }, 401);
  const user = userData.user;

  // Explicit confirmation in the body so a stray POST can never delete an account.
  let body: any;
  try { body = await req.json(); } catch { return json({ error: "bad_request" }, 400); }
  if (body?.confirm !== "DELETE") return json({ error: "confirmation_required" }, 400);

  if (!SERVICE_KEY) return json({ error: "server_misconfigured" }, 500);
  const svc = createClient(SUPABASE_URL, SERVICE_KEY);

  // 1. Cancel any live Stripe subscription so deletion never keeps billing.
  let stripeCancelled = false;
  try {
    const { data: prof } = await svc.from("profiles")
      .select("stripe_customer_id").eq("id", user.id).maybeSingle();
    if (prof?.stripe_customer_id) {
      stripeCancelled = await cancelStripeSubscriptions(prof.stripe_customer_id);
    } else {
      stripeCancelled = true; // nothing to cancel
    }
  } catch (_e) { /* deletion proceeds; sub may need manual cleanup */ }

  // 2. Scrub PII we hold outside the cascading tables.
  try {
    await svc.from("feedback").update({ email: null }).eq("user_id", user.id);
  } catch (_e) { /* non-fatal */ }

  // 3. Delete the auth user. profiles/cloud_usage cascade; feedback.user_id -> null.
  const { error: delErr } = await svc.auth.admin.deleteUser(user.id);
  if (delErr) return json({ error: "delete_failed", detail: (delErr.message ?? "").slice(0, 120) }, 500);

  return json({ ok: true, stripe_cancelled: stripeCancelled });
});
