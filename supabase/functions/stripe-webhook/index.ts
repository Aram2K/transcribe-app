// Stripe webhook → Supabase entitlement sync.
//
// Security model:
//   * Verifies the Stripe signature with STRIPE_WEBHOOK_SECRET (raw body).
//   * Writes to the `subscriptions` table with the SERVICE ROLE key (bypasses
//     RLS) — the only path allowed to grant Pro. The client never can.
//   * Links a Stripe customer to a Supabase user by `client_reference_id`
//     (the app passes the signed-in user's id into Checkout), falling back to
//     matching the email on the `profiles` row.
//
// Required secrets (set in Supabase → Edge Functions → Secrets):
//   STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET
// Auto-provided by Supabase: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
//
// Deploy with JWT verification DISABLED (Stripe can't send a Supabase JWT).

import Stripe from "https://esm.sh/stripe@16?target=deno";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const stripe = new Stripe(Deno.env.get("STRIPE_SECRET_KEY") ?? "", {
  apiVersion: "2024-06-20",
  httpClient: Stripe.createFetchHttpClient(),
});
const webhookSecret = Deno.env.get("STRIPE_WEBHOOK_SECRET") ?? "";
const supabase = createClient(
  Deno.env.get("SUPABASE_URL") ?? "",
  Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "",
);

const MONTHLY_PRICE = "price_1TdvoiCdwD0umc3Ley0fYNFw";
const ANNUAL_PRICE = "price_1TdvojCdwD0umc3LoypQVuS0";

function planForPrice(priceId?: string | null, interval?: string | null): string | null {
  if (priceId === MONTHLY_PRICE) return "monthly";
  if (priceId === ANNUAL_PRICE) return "annual";
  if (interval === "month") return "monthly";
  if (interval === "year") return "annual";
  return null;
}

async function userIdForEmail(email?: string | null): Promise<string | null> {
  if (!email) return null;
  const { data } = await supabase
    .from("profiles")
    .select("id")
    .ilike("email", email)
    .maybeSingle();
  return data?.id ?? null;
}

async function upsertSubscription(sub: Stripe.Subscription, userId: string) {
  const item = sub.items.data[0];
  const priceId = item?.price?.id ?? null;
  const interval = item?.price?.recurring?.interval ?? null;

  await supabase.from("subscriptions").upsert({
    id: sub.id,
    user_id: userId,
    status: sub.status,
    price_id: priceId,
    plan: planForPrice(priceId, interval),
    current_period_end: new Date(sub.current_period_end * 1000).toISOString(),
    cancel_at_period_end: sub.cancel_at_period_end,
    updated_at: new Date().toISOString(),
  });

  if (sub.customer) {
    await supabase
      .from("profiles")
      .update({ stripe_customer_id: String(sub.customer) })
      .eq("id", userId);
  }
}

async function resolveUserId(customerId: string): Promise<string | null> {
  const { data } = await supabase
    .from("profiles")
    .select("id")
    .eq("stripe_customer_id", customerId)
    .maybeSingle();
  if (data?.id) return data.id;
  try {
    const cust = await stripe.customers.retrieve(customerId);
    return await userIdForEmail((cust as Stripe.Customer).email);
  } catch {
    return null;
  }
}

Deno.serve(async (req) => {
  const sig = req.headers.get("stripe-signature");
  const body = await req.text();

  let event: Stripe.Event;
  try {
    event = await stripe.webhooks.constructEventAsync(body, sig ?? "", webhookSecret);
  } catch (err) {
    return new Response(`Signature verification failed: ${(err as Error).message}`, { status: 400 });
  }

  try {
    switch (event.type) {
      case "checkout.session.completed": {
        const session = event.data.object as Stripe.Checkout.Session;
        let userId = session.client_reference_id ?? null;
        if (!userId) {
          userId = await userIdForEmail(
            session.customer_details?.email ?? session.customer_email ?? null,
          );
        }
        if (userId && session.subscription) {
          const sub = await stripe.subscriptions.retrieve(String(session.subscription));
          await upsertSubscription(sub, userId);
        }
        break;
      }
      case "customer.subscription.created":
      case "customer.subscription.updated":
      case "customer.subscription.deleted": {
        const sub = event.data.object as Stripe.Subscription;
        const userId = await resolveUserId(String(sub.customer));
        if (userId) await upsertSubscription(sub, userId);
        break;
      }
      default:
        break;
    }
    return new Response(JSON.stringify({ received: true }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  } catch (err) {
    return new Response(`Handler error: ${(err as Error).message}`, { status: 500 });
  }
});
