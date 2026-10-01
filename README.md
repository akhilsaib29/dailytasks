# Voice Daybook

Send a voice note or text to your Telegram bot, in any language. It drafts projects and tasks (with descriptions, dates and priority) and saves them only after you tap **✅ Proceed**. The dashboard shows Today, Backlog, Board, Week, Month and Projects views, organized as Area > Project > Task.

**Stack (all free, no card):** Groq (open-source Whisper large-v3 + Llama 3.3) · Supabase (Postgres) · Vercel (hosting) · Telegram Bot API.

## Setup (about 15 minutes)

1. **Supabase:** create a project, open **SQL Editor**, and run `schema.sql`. To load existing tasks, also run your private `seed.sql`. Then go to Project Settings → API and copy the **Project URL** and the **service_role** key.
2. **Groq:** go to console.groq.com → API Keys and create a key.
3. **Telegram:** message @BotFather, send `/newbot` and copy the token.
4. **Vercel:** import this GitHub repo (Add New → Project). Under Environment Variables, add everything from `.env.example`. Make up your own values for `TELEGRAM_SECRET` and `DASHBOARD_KEY`. Leave `ALLOWED_CHAT_ID` empty for now. Deploy.
5. Open `https://<your-app>.vercel.app/api/setup?key=<DASHBOARD_KEY>`. The response should show `"ok": true`.
6. In Telegram, send `/start` to your bot. Copy the chat id it replies with into `ALLOWED_CHAT_ID` in Vercel, then redeploy.
7. Open `https://<your-app>.vercel.app` and enter your `DASHBOARD_KEY`.

## Using it

- **Voice or text** → the bot drafts the work → **✅ Proceed** saves it, **✏️ Edit** lets you send corrections (text or voice) and redrafts, **❌ Cancel** discards it.
- `/today` and `/week` list open tasks in Telegram.
- On the dashboard, change a task's status from its pill, or click the task to edit its title, description, status, priority, date or project.

## Local preview

```bash
python -m http.server 8765
```

Put a `seed.json` next to `index.html` to preview with real data without the backend. This file is git-ignored.

## Files

| File | What it does |
|---|---|
| `api/index.py` | Telegram webhook, Groq transcription and drafting, Supabase storage, dashboard API |
| `index.html` | Dashboard (vanilla JS, no build step) |
| `schema.sql` | Database tables |
| `vercel.json` | Routes `/api/*` to the Python function |
