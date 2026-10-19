// Fletcher Fortis inbound mail: Cloudflare Email Routing -> this Email Worker -> POST raw message to the app.
// Worker Settings -> Variables and Secrets (the setup wizard, step 4, shows your exact values with copy buttons):
//   APP_URL        (Text)   = https://<your app address>/inbound/email
//   INBOUND_SECRET (Secret) = the value shown in setup step 4 / Heartbeat -> Settings -> Email
// Optional: FORWARD_TO = a verified Email Routing destination address that also gets a copy of every message.
// Optional: CF_ACCESS_CLIENT_ID + CF_ACCESS_CLIENT_SECRET (Secrets) = a Cloudflare Access service token. Needed when the app
//   sits behind Cloudflare Access (e.g. a home server behind a family login): Access would otherwise answer the Worker
//   with a login page. Add a Service Auth (or Bypass) policy for /inbound/* that accepts this token.
export default {
  async email(message, env, ctx) {
    const raw = await new Response(message.raw).arrayBuffer();
    if (raw.byteLength > 10_000_000) { message.setReject("Message too large"); return; }
    let ok = false;
    try {
      const headers = { "Content-Type": "message/rfc822", "X-Inbound-Secret": env.INBOUND_SECRET,
                        "X-Envelope-From": message.from, "X-Envelope-To": message.to };
      if (env.CF_ACCESS_CLIENT_ID && env.CF_ACCESS_CLIENT_SECRET) {
        headers["CF-Access-Client-Id"] = env.CF_ACCESS_CLIENT_ID; headers["CF-Access-Client-Secret"] = env.CF_ACCESS_CLIENT_SECRET;
      }
      const r = await fetch(env.APP_URL, {
        method: "POST", headers, redirect: "manual",   // an Access login redirect counts as a failure, not a success
        body: raw,
      });
      ok = r.ok;   // 2xx only; a 302 to the Access login page is not ok
      if (!ok) console.log("app returned", r.status, await r.text());
    } catch (e) { console.log("POST failed", e); }
    if (env.FORWARD_TO) await message.forward(env.FORWARD_TO);
    // If the home server is down, reject with a temporary-style message so the broker's server retries later.
    else if (!ok) message.setReject("Temporarily unavailable, please retry later");
  },
};
