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

## Zoho Projects (source of truth for tasks)

Mumbo writes to one Zoho Projects project. On ✅ Proceed it updates existing tasks (status, due date), creates missing ones in the right task list with a title, description, tags and you as owner, adds a dated comment, and records the day in the work log. The dashboard mirrors Zoho; it syncs at most every 5 minutes, or on demand with the ⟳ button or `/sync`.

1. In https://api-console.zoho.in, open **Self Client** and copy the Client ID and Client Secret into Vercel as `ZOHO_CLIENT_ID` and `ZOHO_CLIENT_SECRET`. Also add `ZOHO_PORTAL_ID`, `ZOHO_PROJECT_ID` and `ZOHO_OWNER_ZPUID`, then redeploy.
2. On the Self Client's **Generate Code** tab, paste this scope, choose 10 minutes and click Create:
   `ZohoProjects.portals.READ,ZohoProjects.projects.READ,ZohoProjects.tasklists.READ,ZohoProjects.tasks.ALL,ZohoProjects.tags.READ`
3. Within those minutes, open `https://<your-app>.vercel.app/api/zoho/connect?key=<DASHBOARD_KEY>&code=<the code>`. Copy the `ZOHO_REFRESH_TOKEN` it shows into Vercel, then redeploy.

## Using it

- **Voice or text** → the bot drafts the work → **✅ Proceed** saves it, **✏️ Edit** lets you send corrections (text or voice) and redrafts, **❌ Cancel** discards it.
- `/report today|yesterday|week|lastweek|2026-10-07` gives a day-wise work summary to forward to your manager. `/today` and `/week` list due tasks, and `/sync` pulls from Zoho.
- Dashboard → **Work Log**: day or week view, **Copy report for ED**, and **Export** (CSV) on every view.
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
