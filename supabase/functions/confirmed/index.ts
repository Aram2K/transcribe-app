const HTML = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Transcribe - Email confirmed</title>
<style>
  * { box-sizing: border-box; }
  body { margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center; font-family: 'Segoe UI', system-ui, sans-serif; background: linear-gradient(135deg, #f3e8ff 0%, #eef2ff 50%, #faf5ff 100%); }
  .card { width: 90%; max-width: 440px; text-align: center; padding: 40px 32px; background: rgba(255,255,255,0.9); border-radius: 22px; box-shadow: 0 20px 60px rgba(126,34,206,0.18); }
  .badge { width: 76px; height: 76px; margin: 0 auto 18px; border-radius: 50%; display: flex; align-items: center; justify-content: center; background: linear-gradient(135deg, #a855f7, #7e22ce); color: #fff; font-size: 38px; }
  h1 { font-size: 24px; color: #7e22ce; }
  p { color: #475569; font-size: 15px; }
  .hint { margin-top: 22px; padding: 12px 16px; border-radius: 12px; background: rgba(168,85,247,0.08); color: #6b21a8; font-weight: 600; }
  .foot { margin-top: 18px; font-size: 12px; color: #94a3b8; }
  a { color: #a855f7; text-decoration: none; font-weight: 600; }
</style>
</head>
<body>
  <div class="card">
    <div class="badge">✓</div>
    <h1>Email confirmed</h1>
    <p>Your Transcribe account is verified. You're all set!</p>
    <div class="hint">Return to the Transcribe app and sign in to start dictating.</div>
    <p class="foot">Transcribe by <a href="https://aibuben.xyz">Aibuben</a> &middot; You can close this tab.</p>
  </div>
</body>
</html>`;

Deno.serve(() =>
  new Response(HTML, {
    headers: {
      "Content-Type": "text/html; charset=utf-8",
      "X-Content-Type-Options": "nosniff",
    },
  })
);