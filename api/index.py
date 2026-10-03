"""Voice Daybook: Telegram voice/text -> draft projects & tasks -> Proceed -> Supabase -> dashboard."""
import json
import os
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse

E = lambda k, d="": os.environ.get(k, d)
TG = f"https://api.telegram.org/bot{E('TELEGRAM_TOKEN')}"
TG_FILE = f"https://api.telegram.org/file/bot{E('TELEGRAM_TOKEN')}"
GROQ = "https://api.groq.com/openai/v1"
SB = E("SUPABASE_URL").rstrip("/") + "/rest/v1"
IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI()
http = httpx.Client(timeout=60)


# ---------- services ----------
def sb(method, path, body=None, prefer="return=representation"):
    key = E("SUPABASE_SERVICE_KEY")
    r = http.request(method, f"{SB}/{path}", json=body,
                     headers={"apikey": key, "Authorization": f"Bearer {key}", "Prefer": prefer})
    r.raise_for_status()
    return r.json() if r.content else []


def tg(method, **params):
    return http.post(f"{TG}/{method}", json=params).json()


def send(chat, text, buttons=None):
    # ponytail: hard cut at 4000 chars can split an HTML tag; fine for <10 tasks per message
    p = {"chat_id": chat, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": True}
    if buttons:
        p["reply_markup"] = {"inline_keyboard": buttons}
    return tg("sendMessage", **p)


def groq_auth():
    return {"Authorization": f"Bearer {E('GROQ_API_KEY')}"}


def transcribe(data, filename):
    r = http.post(f"{GROQ}/audio/transcriptions", headers=groq_auth(), files={"file": (filename, data)},
                  data={"model": E("WHISPER_MODEL", "whisper-large-v3"), "response_format": "json"})
    r.raise_for_status()
    return r.json()["text"].strip()


def ask_llm(prompt):
    r = http.post(f"{GROQ}/chat/completions", headers=groq_auth(), json={
        "model": E("GROQ_MODEL", "llama-3.3-70b-versatile"), "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "user", "content": prompt}]})
    r.raise_for_status()
    return json.loads(r.json()["choices"][0]["message"]["content"])


# ---------- drafting ----------
PROMPT = """You organize a person's spoken or typed notes into projects and tasks.
The notes can be in any language or a mix (Hindi, Hinglish, Marathi, Tamil, English, ...).
Today is {today} ({weekday}), India time. Weeks start Monday.
Work is organized as Area > Project > Task. Existing (Area > Project):
{projects}
Rules:
- Put each task in the best-matching existing project. Only create a new project when nothing fits: short name (2-4 words, Title Case) under an existing area (or a new area if truly none fits). Use area "General", project "General" for loose personal items.
- Split compound sentences into separate tasks. Title starts with a verb, under 80 characters.
- description: 1-2 sentences of useful context from the notes (who, what, why). Never invent facts.
- Resolve relative dates (tomorrow, Friday, next week = next Monday, end of month) to YYYY-MM-DD. No date mentioned = today.
- priority: "high" if urgent or due within 2 days, "low" if it can wait, else "med".
- Write titles, descriptions and the summary in {language}.
{revision}
Reply with only a JSON object:
{{"summary": "one sentence", "projects": [{{"name": "", "area": "", "description": "one-line goal"}}], "tasks": [{{"title": "", "description": "", "project": "", "date": "YYYY-MM-DD", "priority": "high|med|low"}}]}}
Every project used by a task must be listed in "projects".

Notes:
\"\"\"
{notes}
\"\"\""""


def today():
    return datetime.now(IST).date()


def build_prompt(notes, existing, current=None):
    """existing: {project name: area}"""
    revision = ("This is a REVISION. Current draft:\n" + json.dumps(current, ensure_ascii=False) +
                "\nApply the corrections in the notes below and return the full updated draft.") if current else ""
    t = today()
    listing = "\n".join(f"- {a} > {p}" for p, a in sorted(existing.items(), key=lambda x: (x[1], x[0])))
    return PROMPT.format(today=t.isoformat(), weekday=t.strftime("%A"), projects=listing or "none",
                         language=E("OUTPUT_LANGUAGE", "English"), revision=revision, notes=notes[:20000])


def valid_date(s, fallback):
    try:
        return datetime.strptime(str(s), "%Y-%m-%d").date().isoformat()
    except ValueError:
        return fallback


def clean_draft(d, fallback_date, existing=None):
    """Normalize whatever the LLM returned into a safe draft."""
    tasks = []
    for t in d.get("tasks") or []:
        if not isinstance(t, dict) or not str(t.get("title") or "").strip():
            continue
        tasks.append({
            "title": str(t["title"]).strip()[:200],
            "description": str(t.get("description") or "").strip()[:500],
            "project": str(t.get("project") or "").strip()[:60] or "General",
            "date": valid_date(t.get("date"), fallback_date),
            "priority": t.get("priority") if t.get("priority") in ("high", "med", "low") else "med",
        })
    existing = existing or {}
    meta = {str(p.get("name") or "").strip(): p for p in d.get("projects") or [] if isinstance(p, dict)}
    projects = [{"name": n,
                 "area": existing.get(n) or str(meta.get(n, {}).get("area") or "").strip()[:60] or "General",
                 "description": str(meta.get(n, {}).get("description") or "").strip()[:300]}
                for n in dict.fromkeys(t["project"] for t in tasks)]
    return {"summary": str(d.get("summary") or "").strip()[:300], "projects": projects, "tasks": tasks}


def format_draft(d, existing):
    out = [f"📝 <b>Draft</b>: {escape(d['summary'])}", ""]
    for p in d["projects"]:
        out.append(f"📁 <b>{escape(p['name'])}</b> · {escape(p['area'])}" + ("" if p["name"] in existing else " <i>(new)</i>"))
        if p["description"]:
            out.append(f"<i>{escape(p['description'])}</i>")
        for t in (t for t in d["tasks"] if t["project"] == p["name"]):
            day = datetime.strptime(t["date"], "%Y-%m-%d").strftime("%a %d %b")
            out.append(f"  ☐ <b>{escape(t['title'])}</b> · {day} · {t['priority'].upper()}")
            if t["description"]:
                out.append(f"      {escape(t['description'])}")
        out.append("")
    return "\n".join(out)


def buttons(draft_id):
    return [[{"text": "✅ Proceed", "callback_data": f"p:{draft_id}"},
             {"text": "✏️ Edit", "callback_data": f"e:{draft_id}"},
             {"text": "❌ Cancel", "callback_data": f"c:{draft_id}"}]]


def task_list(until, title):
    rows = sb("GET", f"tasks?done=eq.false&date=lte.{until}&order=date")
    if not rows:
        return f"<b>{title}</b>\nNothing open. 🎉"
    t = today().isoformat()
    lines = [f"<b>{title}</b>"]
    for r in rows:
        late = " ⚠️" if r["date"] < t else ""
        lines.append(f"☐ {escape(r['title'])} · <i>{escape(r['project'])}</i> · {r['date'][5:]}{late}")
    return "\n".join(lines)


# ---------- telegram handlers ----------
def allowed(chat):
    return E("ALLOWED_CHAT_ID") and str(chat) == E("ALLOWED_CHAT_ID")


def handle_message(msg):
    chat = msg["chat"]["id"]
    text = (msg.get("text") or msg.get("caption") or "").strip()
    if text.startswith("/start"):
        return send(chat, f"Hi! Your chat id is <code>{chat}</code>.\nPut it in ALLOWED_CHAT_ID, then send me a voice "
                          "note or text in any language. Commands: /today /week")
    if not allowed(chat):
        return
    if text.startswith("/today"):
        return send(chat, task_list(today().isoformat(), "Today + overdue"))
    if text.startswith("/week"):
        t = today()
        return send(chat, task_list((t + timedelta(days=6 - t.weekday())).isoformat(), "This week + overdue"))

    media = msg.get("voice") or msg.get("audio") or msg.get("video_note")
    if media:
        tg("sendChatAction", chat_id=chat, action="typing")
        path = tg("getFile", file_id=media["file_id"])["result"]["file_path"]
        text = transcribe(http.get(f"{TG_FILE}/{path}").content, Path(path).name.replace(".oga", ".ogg"))
        send(chat, f"🎙 <i>{escape(text[:1500])}</i>")
    if not text:
        return send(chat, "Send me a voice note or a text message.")

    tg("sendChatAction", chat_id=chat, action="typing")
    existing = {p["name"]: p["area"] for p in sb("GET", "projects?select=name,area")}
    editing = sb("GET", f"drafts?chat_id=eq.{chat}&status=eq.editing&order=id.desc&limit=1")
    if editing:
        old = editing[0]
        draft = clean_draft(ask_llm(build_prompt(text, existing, old["draft"])), today().isoformat(), existing)
        row = sb("PATCH", f"drafts?id=eq.{old['id']}",
                 {"draft": draft, "status": "pending", "source": old["source"] + "\n\nCorrection: " + text})[0]
    else:
        draft = clean_draft(ask_llm(build_prompt(text, existing)), today().isoformat(), existing)
        if not draft["tasks"]:
            return send(chat, "I couldn't find any tasks in that. Say what needs doing and when.")
        row = sb("POST", "drafts", {"chat_id": chat, "source": text, "draft": draft})[0]
    send(chat, format_draft(draft, existing), buttons(row["id"]))


def handle_callback(cb):
    tg("answerCallbackQuery", callback_query_id=cb["id"])
    chat, mid = cb["message"]["chat"]["id"], cb["message"]["message_id"]
    if not allowed(chat):
        return
    action, draft_id = cb["data"].split(":")
    tg("editMessageReplyMarkup", chat_id=chat, message_id=mid, reply_markup={"inline_keyboard": []})
    if action == "e":  # only one draft can be waiting for corrections
        sb("PATCH", f"drafts?chat_id=eq.{chat}&status=eq.editing", {"status": "cancelled"}, prefer="return=minimal")
    status = {"p": "saved", "e": "editing", "c": "cancelled"}[action]
    # The status=pending filter makes a double tap a no-op instead of saving twice.
    rows = sb("PATCH", f"drafts?id=eq.{int(draft_id)}&status=eq.pending", {"status": status})
    if not rows:
        return
    d = rows[0]["draft"]
    if action == "p":
        try:
            sb("POST", "projects?on_conflict=name", d["projects"], prefer="resolution=ignore-duplicates,return=minimal")
            sb("POST", "tasks", d["tasks"], prefer="return=minimal")
        except Exception:
            sb("PATCH", f"drafts?id=eq.{int(draft_id)}", {"status": "pending"}, prefer="return=minimal")
            send(chat, "⚠️ Saving failed. The draft is kept, tap Proceed again.", buttons(draft_id))
            raise
        send(chat, f"✅ Saved {len(d['tasks'])} tasks in {len(d['projects'])} projects.")
    elif action == "e":
        send(chat, "✏️ Send corrections as text or voice, e.g. \"move the HFC call to Monday, drop the domain task\".")
    else:
        send(chat, "❌ Draft discarded.")


# ---------- routes ----------
@app.post("/api/telegram")
def telegram(update: dict, x_telegram_bot_api_secret_token: str = Header("")):
    if not E("TELEGRAM_SECRET") or x_telegram_bot_api_secret_token != E("TELEGRAM_SECRET"):
        raise HTTPException(403)
    msg = update.get("message") or (update.get("callback_query") or {}).get("message") or {}
    try:
        if "callback_query" in update:
            handle_callback(update["callback_query"])
        elif "message" in update:
            handle_message(update["message"])
    except Exception as e:  # always 200, or Telegram retries the same update forever
        chat = msg.get("chat", {}).get("id")
        if allowed(chat):
            send(chat, f"⚠️ Something failed: {escape(str(e))[:300]}")
    return {"ok": True}


def check(key):
    if not E("DASHBOARD_KEY") or key != E("DASHBOARD_KEY"):
        raise HTTPException(401)


@app.get("/api/data")
def data(x_key: str = Header("")):
    check(x_key)
    logs = sb("GET", "drafts?status=eq.saved&select=source,draft,created_at&order=id.desc&limit=300")
    return {"tasks": sb("GET", "tasks?order=date"), "projects": sb("GET", "projects"),
            "logs": [{"summary": l["draft"].get("summary", ""), "count": len(l["draft"].get("tasks", [])),
                      "text": l["source"], "created_at": l["created_at"]} for l in logs]}


@app.patch("/api/tasks/{tid}")
def update_task(tid: int, body: dict, x_key: str = Header("")):
    check(x_key)
    patch = {k: body[k] for k in ("title", "description", "status", "priority", "date", "project") if k in body}
    if "status" in patch:
        patch["done"] = patch["status"] in ("done", "cancelled")
    if "date" in patch:
        patch["date"] = valid_date(patch["date"], None)
    if patch:
        sb("PATCH", f"tasks?id=eq.{tid}", patch, prefer="return=minimal")
    return {"ok": True}


@app.delete("/api/tasks/{tid}")
def delete_task(tid: int, x_key: str = Header("")):
    check(x_key)
    sb("DELETE", f"tasks?id=eq.{tid}", prefer="return=minimal")
    return {"ok": True}


@app.get("/api/setup")
def setup(key: str, request: Request):
    """Open once after deploy: points the Telegram bot at this server."""
    check(key)
    url = E("PUBLIC_URL") or f"https://{request.headers['host']}"
    return tg("setWebhook", url=f"{url}/api/telegram", secret_token=E("TELEGRAM_SECRET"),
              allowed_updates=["message", "callback_query"])


@app.middleware("http")
async def vercel_path(request: Request, call_next):
    # Vercel's rewrite hands every call over as /api/index; vercel.json passes the real path in ?__path=
    real = request.query_params.get("__path")
    if real is not None and request.url.path == "/api/index":
        request.scope["path"] = "/api/" + real
    return await call_next(request)


@app.exception_handler(404)
def not_found(request: Request, exc):
    from fastapi.responses import JSONResponse
    return JSONResponse({"detail": "Not Found", "path": request.url.path}, status_code=404)


@app.get("/")
def home():  # Vercel serves index.html itself; this is for local runs
    return FileResponse(Path(__file__).parent.parent / "index.html")


if __name__ == "__main__":
    d = clean_draft({"summary": "x", "projects": [{"name": "Steel Report", "area": "Wrong", "description": "launch"}],
                     "tasks": [{"title": "Call Ravi", "project": "Steel Report", "date": "2026-10-02", "priority": "high"},
                               {"title": "Renew domain", "date": "next week", "priority": "urgent"},
                               {"title": "  "}, "junk"]}, "2026-10-01", {"Steel Report": "Research"})
    assert [t["title"] for t in d["tasks"]] == ["Call Ravi", "Renew domain"]
    assert d["tasks"][1] == {"title": "Renew domain", "description": "", "project": "General",
                             "date": "2026-10-01", "priority": "med"}
    assert [p["name"] for p in d["projects"]] == ["Steel Report", "General"]
    assert d["projects"][0] == {"name": "Steel Report", "area": "Research", "description": "launch"}
    assert d["projects"][1]["area"] == "General"
    assert "(new)" in format_draft(d, {"Steel Report": "Research"}).split("📁 <b>General")[1][:40]
    assert valid_date("2026-13-40", "f") == "f"
    print("self-check ok")
