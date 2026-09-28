// Stripe Customer Portal for the signed-in user: "Manage subscription" in the
// app opens it already signed in - cancel (at the end of the period, so a
// trial is never charged), change the card, download invoices.
//
// Flow: verify the caller's JWT -> their Stripe customer id (profiles, set by
// stripe-webhook; else the Stripe customer with their VERIFIED email, which is
// then remembered) -> a portal session URL for that customer only.
//
// Secrets: STRIPE_SECRET_KEY (shared with stripe-webhook). SUPABASE_URL,
// SUPABASE_ANON_KEY and SUPABASE_SERVICE_ROLE_KEY are provided by Supabase.
// Optional: BILLING_RETURN_URL (where the portal's "Return" link goes).
//
// Deploy with JWT verification DISABLED - it checks the JWT itself, like the
// other functions.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL") ?? "";
const ANON_KEY = Deno.env.get("SUPABASE_ANON_KEY") ?? "";
const SERVICE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";
const STRIPE_KEY = Deno.env.get("STRIPE_SECRET_KEY") ?? "";
const RETURN_URL = Deno.env.get("BILLING_RETURN_URL") ?? "https://aibuben.xyz/transcribe";

const cors = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

function json(obj: unknown, status = 200) {
  return new Response(JSON.stringify(obj), { status, headers: { ...cors, "Content-Type": "application/json" } });
}

function stripe(path: string, init: RequestInit = {}) {
  return fetch(`https://api.stripe.com/v1/${path}`, {
    ...init,
    headers: { Authorization: `Bearer ${STRIPE_KEY}`, ...(init.headers ?? {}) },
  });
}

// Stripe's email filter is case-sensitive: try the address as stored, then
// lower-cased. Newest customer first (Stripe lists newest first).
async function customerForEmail(email: string): Promise<string | null> {
  for (const candidate of new Set([email, email.toLowerCase()])) {
    const r = await stripe(`customers?email=${encodeURIComponent(candidate)}&limit=1`);
    if (!r.ok) continue;
    const data = await r.json();
    if (data?.data?.[0]?.id) return data.data[0].id;
  }
  return null;
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors });
  if (req.method !== "POST") return json({ error: "method_not_allowed" }, 405);

  const authHeader = req.headers.get("Authorization") ?? "";
  if (!authHeader.startsWith("Bearer ")) return json({ error: "unauthorized" }, 401);
  if (!STRIPE_KEY || !SERVICE_KEY) return json({ error: "server_misconfigured" }, 503);

  const supa = createClient(SUPABASE_URL, ANON_KEY, { global: { headers: { Authorization: authHeader } } });
  const { data: userData, error: userErr } = await supa.auth.getUser();
  if (userErr || !userData?.user) return json({ error: "unauthorized" }, 401);
  const user = userData.user;

  const svc = createClient(SUPABASE_URL, SERVICE_KEY);
  const { data: prof } = await svc.from("profiles")
    .select("stripe_customer_id").eq("id", user.id).maybeSingle();
  let customer: string | null = prof?.stripe_customer_id ?? null;

  // Bought before the webhook could link the account (e.g. on the website):
  // find the customer by email - only a verified one, or anybody could sign
  // up with someone else's address and open their billing. (Relies on
  // Supabase Auth's "Confirm email" staying on: with it off, every sign-up
  // counts as confirmed.)
  if (!customer && user.email && user.email_confirmed_at) {
    customer = await customerForEmail(user.email);
    if (customer) {
      await svc.from("profiles").update({ stripe_customer_id: customer }).eq("id", user.id);
    }
  }
  if (!customer) return json({ error: "no_customer" }, 404);

  const r = await stripe("billing_portal/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ customer, return_url: RETURN_URL }),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok || !data?.url) {
    console.error("billing portal session failed", r.status, data?.error?.message ?? "");
    return json({ error: "portal_failed" }, 502);
  }
  return json({ url: data.url });
});
