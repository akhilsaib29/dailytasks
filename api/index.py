"""Mumbo: Telegram voice/text -> draft -> Proceed -> tasks + dated work log in Supabase -> dashboard."""
import json
import os
import re
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

E = lambda k, d="": os.environ.get(k, d).strip()
TG = f"https://api.telegram.org/bot{E('TELEGRAM_TOKEN')}"
TG_FILE = f"https://api.telegram.org/file/bot{E('TELEGRAM_TOKEN')}"
GROQ = "https://api.groq.com/openai/v1"
SB = E("SUPABASE_URL").rstrip("/") + "/rest/v1"
IST = timezone(timedelta(hours=5, minutes=30))
PRIORITIES = ("high", "med", "low")
# Names speech-to-text gets wrong. Right spelling -> what it was heard as. Extend via VOCAB env: "Name=miss1|miss2;..."
VOCAB = {"Zoho": ["Jovo", "Joho", "Zojo", "Joe Ho"], "Mumbo": ["Mambo", "Mumbu"], "Suyash": ["Suyas", "Sooyash"],
         **{k.strip(): v.split("|") for k, _, v in (x.partition("=") for x in E("VOCAB").split(";")) if k.strip()}}
MISHEARD = re.compile(r"\b(" + "|".join(re.escape(w) for v in VOCAB.values() for w in v) + r")\b", re.I)
RIGHT = {w.lower(): k for k, v in VOCAB.items() for w in v}
MARK = {"done": "✅", "in_progress": "🔄", "in_review": "👀", "on_hold": "⏸", "cancelled": "✖️", "open": "☐"}
LABEL = {"open": "Open", "in_progress": "In progress", "in_review": "In review", "on_hold": "On hold",
         "done": "Done", "cancelled": "Cancelled"}

app = FastAPI()
http = httpx.Client(timeout=60)


# ---------- services ----------
def sb(method, path, body=None, prefer="return=representation"):
    key = E("SUPABASE_SERVICE_KEY")
    r = http.request(method, f"{SB}/{path}", json=body,
                     headers={"apikey": key, "Authorization": f"Bearer {key}", "Prefer": prefer})
    if r.is_error:
        raise RuntimeError(f"Database {r.status_code}: {r.text[:300]}")
    return r.json() if r.content else []


def tg(method, **params):
    return http.post(f"{TG}/{method}", json=params).json()


def send(chat, text, buttons=None):
    # ponytail: hard cut at 4000 chars can split an HTML tag; fine for ~10 items per message
    p = {"chat_id": chat, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": True}
    if buttons:
        p["reply_markup"] = {"inline_keyboard": buttons}
    return tg("sendMessage", **p)


def fix_names(text):
    return MISHEARD.sub(lambda m: RIGHT[m.group(0).lower()], text)


def groq_auth():
    return {"Authorization": f"Bearer {E('GROQ_API_KEY')}"}


def transcribe(data, filename):
    r = http.post(f"{GROQ}/audio/transcriptions", headers=groq_auth(), files={"file": (filename, data)},
                  data={"model": E("WHISPER_MODEL", "whisper-large-v3"), "response_format": "json",
                        "prompt": "Work update mentioning " + ", ".join(VOCAB) + "."})
    if r.is_error:
        raise RuntimeError(f"Groq {r.status_code}: {r.text[:300]}")
    return fix_names(r.json()["text"].strip())


def ask_llm(prompt):
    # Groq retires models now and then: try the configured one, then known-good fallbacks.
    models = dict.fromkeys(m for m in (E("GROQ_MODEL"), "llama-3.3-70b-versatile", "openai/gpt-oss-120b", "llama-3.1-8b-instant") if m)
    for model in models:
        r = http.post(f"{GROQ}/chat/completions", headers=groq_auth(), json={
            "model": model, "temperature": 0.2, "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": prompt}]})
        if r.status_code not in (400, 404) or "model" not in r.text:
            break
    if r.is_error:
        raise RuntimeError(f"Groq {r.status_code}: {r.text[:300]}")
    return json.loads(r.json()["choices"][0]["message"]["content"])


# ---------- drafting ----------
PROMPT = """You turn a person's spoken or typed work update into a work log against their task list.
The notes can be in any language or a mix (Hindi, Hinglish, Marathi, Tamil, English, ...).
Today is {today} ({weekday}), India time. Weeks start Monday.

Existing tasks (number | task list | status | title):
{tasks}

Task lists: {lists}

Rules:
- Make one item per piece of work mentioned. Split compound sentences.
- If an item is the same work as an existing task, set "match" to that task's number. Otherwise "match": null and write a new task: title (starts with a verb, under 80 chars), description (2-3 sentences of useful context from the notes, never invent facts), tasklist (exactly one of the task lists).
- note: one sentence in past tense of what the person did or said about it, for their daily work log.
- status after this update: "done" if finished, "in_progress" if worked on and not finished, "in_review" if waiting on review/approval, "on_hold" if paused/blocked, "open" for new work not started.
- due: YYYY-MM-DD only if a deadline is stated (resolve "Friday", "next week" = next Monday), else null.
- priority: "high" if urgent, "low" if it can wait, else "med".
- date: the day the work happened, YYYY-MM-DD (today unless they say yesterday or another day).
- Write titles, descriptions, notes and the summary in {language}.
- The notes come from speech-to-text, which mishears names. Always spell these exactly: {vocab}.
{revision}
EXCEPTION: if the message is not a work update but asks to change the DATE of their previous update
(e.g. "change that to yesterday", "move my last update to Monday", "that was for 5th October"),
reply only {{"action": "redate", "date": "YYYY-MM-DD"}} with the new date resolved.

Otherwise reply with only a JSON object:
{{"summary": "one sentence", "date": "YYYY-MM-DD", "items": [{{"match": 12, "title": "", "description": "", "tasklist": "", "status": "", "priority": "med", "due": null, "note": ""}}]}}

Notes:
\"\"\"
{notes}
\"\"\""""


def today():
    return datetime.now(IST).date()


def candidates():
    """Open tasks plus anything closed in the last 30 days: what a new update could refer to."""
    since = (today() - timedelta(days=30)).isoformat()
    return sb("GET", f"tasks?or=(done.eq.false,completed_at.gte.{since})&select=id,title,project,status&order=project,title")


def task_lists():
    return [p["name"] for p in sb("GET", "projects?select=name&order=name")]


def build_prompt(notes, cands, lists, current=None):
    revision = ("This is a REVISION. Current draft:\n" + json.dumps(current, ensure_ascii=False) +
                "\nApply the corrections in the notes below and return the full updated draft.") if current else ""
    t = today()
    listing = "\n".join(f"{i} | {c['project']} | {LABEL[c['status']]} | {c['title']}" for i, c in enumerate(cands, 1))
    return PROMPT.format(today=t.isoformat(), weekday=t.strftime("%A"), tasks=listing or "none",
                         lists=", ".join(lists), vocab="; ".join(f"{k} (never {' / '.join(v)})" for k, v in VOCAB.items()), language=E("OUTPUT_LANGUAGE", "English"), revision=revision, notes=notes[:20000])


def valid_date(s, fallback):
    try:
        return datetime.strptime(str(s), "%Y-%m-%d").date().isoformat()
    except ValueError:
        return fallback


def clean_draft(d, cands, lists, fallback_date):
    """Normalize whatever the LLM returned into a safe draft: matches must be real tasks, lists must exist."""
    default_list = next((l for l in lists if l.lower().startswith("general")), lists[0] if lists else "")
    items = []
    for it in d.get("items") or []:
        if not isinstance(it, dict):
            continue
        m = it.get("match")
        c = cands[m - 1] if isinstance(m, int) and 1 <= m <= len(cands) else None
        if not c and not str(it.get("title") or "").strip():
            continue
        status = it.get("status") if it.get("status") in LABEL else ("in_progress" if c else "open")
        item = {"status": status, "note": str(it.get("note") or "").strip()[:500],
                "due": valid_date(it.get("due"), None),
                "priority": it.get("priority") if it.get("priority") in PRIORITIES else "med"}
        if c:
            item.update(task_id=c["id"], title=c["title"], tasklist=c["project"], was=c["status"])
        else:
            tl = str(it.get("tasklist") or "").strip()
            item.update(task_id=None, title=str(it["title"]).strip()[:200],
                        description=str(it.get("description") or "").strip()[:2000],
                        tasklist=tl if tl in lists else default_list)
        items.append(item)
    return {"summary": str(d.get("summary") or "").strip()[:300], "date": valid_date(d.get("date"), fallback_date),
            "items": items}


def format_draft(d):
    day = datetime.strptime(d["date"], "%Y-%m-%d").strftime("%a %d %b")
    out = [f"📅 <b>{day}</b>: {escape(d['summary'])}", ""]
    for it in d["items"]:
        due = f" · due {datetime.strptime(it['due'], '%Y-%m-%d').strftime('%d %b')}" if it.get("due") else ""
        if existing(it):
            change = f"{LABEL[it['was']]} → <b>{LABEL[it['status']]}</b>" if it["status"] != it["was"] else LABEL[it["status"]]
            out.append(f"{MARK[it['status']]} <b>{escape(it['title'])}</b>\n     existing · {escape(it['tasklist'])} · {change}{due}")
        else:
            out.append(f"➕ <b>{escape(it['title'])}</b>\n     NEW in {escape(it['tasklist'])} · {LABEL[it['status']]}{due}")
            if it.get("description"):
                out.append(f"     <i>{escape(it['description'])}</i>")
        if it["note"]:
            out.append(f"     📝 {escape(it['note'])}")
        out.append("")
    return "\n".join(out)


def buttons(draft_id):
    return [[{"text": "✅ Proceed", "callback_data": f"p:{draft_id}"},
             {"text": "✏️ Edit", "callback_data": f"e:{draft_id}"},
             {"text": "❌ Cancel", "callback_data": f"c:{draft_id}"}]]


def existing(it):
    return it.get("task_id") or it.get("zoho_id")  # zoho_id: drafts made before Zoho was dropped


def apply_item(it):
    """Write one draft item to the tasks table. Returns the task id."""
    closed = it["status"] in ("done", "cancelled")
    if existing(it):
        key = f"id=eq.{it['task_id']}" if it.get("task_id") else f"zoho_id=eq.{it['zoho_id']}"
        row = sb("GET", f"tasks?{key}&select=id,done")
        if not row:
            raise RuntimeError("task no longer exists")
        patch = {"status": it["status"], "done": closed}
        if row[0]["done"] != closed:
            patch["completed_at"] = today().isoformat() if closed else None
        if it.get("due"):
            patch["date"] = it["due"]
        sb("PATCH", f"tasks?id=eq.{row[0]['id']}", patch, prefer="return=minimal")
        return row[0]["id"]
    sb("POST", "projects?on_conflict=name", {"name": it["tasklist"]}, prefer="resolution=ignore-duplicates,return=minimal")
    return sb("POST", "tasks", {"title": it["title"], "description": it.get("description", ""), "project": it["tasklist"],
                                "status": it["status"], "done": closed, "priority": it["priority"], "date": it.get("due"),
                                "completed_at": today().isoformat() if closed else None})[0]["id"]


def report(arg):
    t, arg = today(), arg.strip().lower()
    if arg in ("", "today"):
        start = end = t
    elif arg == "yesterday":
        start = end = t - timedelta(days=1)
    elif arg == "week":
        start, end = t - timedelta(days=t.weekday()), t
    elif arg in ("lastweek", "last week"):
        start = t - timedelta(days=t.weekday() + 7)
        end = start + timedelta(days=6)
    else:
        d = valid_date(arg, None)
        if not d:
            return "Use /report today, yesterday, week, lastweek or a date like 2026-10-07."
        start = end = datetime.strptime(d, "%Y-%m-%d").date()
    rows = sb("GET", f"worklog?date=gte.{start}&date=lte.{end}&order=date,id")
    if not rows:
        return f"No work logged for {start:%d %b}" + (f" – {end:%d %b}." if end != start else ".")
    out, day = [f"<b>Work log · {start:%d %b}" + (f" – {end:%d %b}" if end != start else "") + "</b>"], None
    for r in rows:
        if r["date"] != day:
            day = r["date"]
            out.append(f"\n<b>{datetime.strptime(day, '%Y-%m-%d'):%a %d %b}</b>")
        out.append(f"• {escape(r['title'])} — {LABEL.get(r['status'], r['status'])}"
                   + (f"\n   {escape(r['note'])}" if r["note"] else ""))
    return "\n".join(out)


def task_list(until, title):
    rows = sb("GET", f"tasks?done=eq.false&date=lte.{until}&order=date")
    if not rows:
        return f"<b>{title}</b>\nNothing due. 🎉"
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
        return send(chat, f"Hi! Your chat id is <code>{chat}</code>.\nSend me a voice note or text in any language about "
                          "what you worked on. Commands: /report [today|yesterday|week|lastweek|date] /today /week")
    if not allowed(chat):
        return
    if text.startswith("/report"):
        return send(chat, report(text[7:]))
    if text.startswith("/today"):
        return send(chat, task_list(today().isoformat(), "Due today + overdue"))
    if text.startswith("/week"):
        t = today()
        return send(chat, task_list((t + timedelta(days=6 - t.weekday())).isoformat(), "Due this week + overdue"))

    media = msg.get("voice") or msg.get("audio") or msg.get("video_note")
    if media:
        tg("sendChatAction", chat_id=chat, action="typing")
        path = tg("getFile", file_id=media["file_id"])["result"]["file_path"]
        text = transcribe(http.get(f"{TG_FILE}/{path}").content, Path(path).name.replace(".oga", ".ogg"))
        send(chat, f"🎙 <i>{escape(text[:1500])}</i>")
    if not text:
        return send(chat, "Send me a voice note or a text message.")

    tg("sendChatAction", chat_id=chat, action="typing")
    lists, cands = task_lists(), candidates()
    editing = sb("GET", f"drafts?chat_id=eq.{chat}&status=eq.editing&order=id.desc&limit=1")
    if editing:
        old = editing[0]
        draft = clean_draft(ask_llm(build_prompt(text, cands, lists, old["draft"])), cands, lists, today().isoformat())
        row = sb("PATCH", f"drafts?id=eq.{old['id']}",
                 {"draft": draft, "status": "pending", "source": old["source"] + "\n\nCorrection: " + text})[0]
    else:
        raw = ask_llm(build_prompt(text, cands, lists))
        if raw.get("action") == "redate":
            return offer_redate(chat, text, valid_date(raw.get("date"), None))
        draft = clean_draft(raw, cands, lists, today().isoformat())
        if not draft["items"]:
            return send(chat, "I couldn't find any work in that. Say what you did or need to do.")
        row = sb("POST", "drafts", {"chat_id": chat, "source": text, "draft": draft})[0]
    send(chat, format_draft(draft), buttons(row["id"]))


def offer_redate(chat, text, new):
    last = sb("GET", f"drafts?chat_id=eq.{chat}&status=eq.saved&draft->>items=not.is.null&draft->>action=is.null&order=id.desc&limit=1")
    if not new or not last:
        return send(chat, "I couldn't tell which update or which date. Try \"move my last update to yesterday\".")
    target, old = last[0], last[0]["draft"]["date"]
    if old == new:
        return send(chat, f"Your last update is already dated {new}.")
    items = [it for it in target["draft"]["items"] if it.get("applied")]
    d = {"action": "redate", "target": target["id"], "from": old, "date": new, "summary": "", "items": []}
    row = sb("POST", "drafts", {"chat_id": chat, "source": text, "draft": d})[0]
    day = lambda x: datetime.strptime(x, "%Y-%m-%d").strftime("%a %d %b")
    lines = [f"📅 Move your last update from <b>{day(old)}</b> to <b>{day(new)}</b>?", ""]
    lines += [f"• {escape(it['title'])}" for it in items]
    send(chat, "\n".join(lines), [[{"text": "✅ Proceed", "callback_data": f"p:{row['id']}"},
                                    {"text": "❌ Cancel", "callback_data": f"c:{row['id']}"}]])


def apply_redate(chat, d):
    target = sb("GET", f"drafts?id=eq.{d['target']}")[0]
    new = d["date"]
    sb("PATCH", f"worklog?draft_id=eq.{d['target']}", {"date": new}, prefer="return=minimal")
    sb("PATCH", f"drafts?id=eq.{d['target']}", {"draft": {**target["draft"], "date": new}}, prefer="return=minimal")
    send(chat, f"✅ Moved to {datetime.strptime(new, '%Y-%m-%d'):%a %d %b}.")


def proceed(chat, draft_id, d):
    done, failed = [], []
    for it in d["items"]:
        if it.get("applied"):
            continue
        try:
            tid = apply_item(it)
            sb("POST", "worklog", {"date": d["date"], "title": it["title"], "tasklist": it["tasklist"],
                                   "note": it["note"], "status": it["status"], "draft_id": draft_id},
               prefer="return=minimal")
            it["applied"] = tid
            done.append(it)
        except Exception as e:
            failed.append((it, str(e)))
        sb("PATCH", f"drafts?id=eq.{draft_id}", {"draft": d}, prefer="return=minimal")  # progress survives a crash
    lines = [f"{'➕' if not existing(it) else MARK[it['status']]} {escape(it['title'])}" for it in done]
    if failed:
        sb("PATCH", f"drafts?id=eq.{draft_id}", {"status": "pending"}, prefer="return=minimal")
        lines += ["", "⚠️ <b>Not saved yet</b> (tap Proceed to retry only these):"]
        lines += [f"• {escape(it['title'])}: {escape(err[:150])}" for it, err in failed]
        return send(chat, "\n".join(lines), buttons(draft_id))
    send(chat, f"✅ Saved to your tasks and work log for {d['date']}:\n" + "\n".join(lines))


def handle_callback(cb):
    tg("answerCallbackQuery", callback_query_id=cb["id"])
    chat, mid = cb["message"]["chat"]["id"], cb["message"]["message_id"]
    if not allowed(chat):
        return
    action, draft_id = cb["data"].split(":")
    draft_id = int(draft_id)
    tg("editMessageReplyMarkup", chat_id=chat, message_id=mid, reply_markup={"inline_keyboard": []})
    if action == "e":  # only one draft can be waiting for corrections
        sb("PATCH", f"drafts?chat_id=eq.{chat}&status=eq.editing", {"status": "cancelled"}, prefer="return=minimal")
    status = {"p": "saved", "e": "editing", "c": "cancelled"}[action]
    # The status=pending filter makes a double tap a no-op instead of saving twice.
    rows = sb("PATCH", f"drafts?id=eq.{draft_id}&status=eq.pending", {"status": status})
    if not rows:
        return
    if action == "p" and rows[0]["draft"].get("action") == "redate":
        apply_redate(chat, rows[0]["draft"])
    elif action == "p":
        proceed(chat, draft_id, rows[0]["draft"])
    elif action == "e":
        send(chat, "✏️ Send corrections as text or voice, e.g. \"the EDR one is for Udit, not Shreya\".")
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
    want = E("DASHBOARD_KEY")  # stray spaces pasted into Vercel shouldn't lock you out
    if not want or key.strip() != want:
        raise HTTPException(401)


@app.get("/api/data")
def data(x_key: str = Header("")):
    check(x_key)
    logs = sb("GET", "drafts?status=eq.saved&select=source,draft,created_at&order=id.desc&limit=300")
    since = (today() - timedelta(days=180)).isoformat()
    return {"tasks": sb("GET", "tasks?order=date"), "projects": sb("GET", "projects"),
            "worklog": sb("GET", f"worklog?date=gte.{since}&order=date.desc,id"),
            "logs": [{"summary": l["draft"].get("summary", ""),
                      "count": len(l["draft"].get("items") or l["draft"].get("tasks") or []),
                      "text": l["source"], "created_at": l["created_at"]} for l in logs]}


@app.patch("/api/tasks/{tid}")
def update_task(tid: int, body: dict, x_key: str = Header("")):
    check(x_key)
    patch = {k: body[k] for k in ("title", "description", "project", "status", "priority", "date") if k in body}
    if "date" in patch:
        patch["date"] = valid_date(patch["date"], None)
    if not patch:
        return {"ok": True}
    row = sb("GET", f"tasks?id=eq.{tid}&select=done")[0]
    if "status" in patch:
        patch["done"] = patch["status"] in ("done", "cancelled")
        if row["done"] != patch["done"]:
            patch["completed_at"] = today().isoformat() if patch["done"] else None
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


@app.exception_handler(RuntimeError)
def upstream_error(request: Request, exc):  # Database/Groq errors reach the dashboard with their real message
    return JSONResponse({"detail": str(exc)}, status_code=502)


@app.exception_handler(404)
def not_found(request: Request, exc):
    return JSONResponse({"detail": "Not Found", "path": request.url.path}, status_code=404)


@app.get("/")
def home():  # Vercel serves index.html itself; this is for local runs
    return FileResponse(Path(__file__).parent.parent / "index.html")


if __name__ == "__main__":
    lists = ["MSOC", "General ++"]
    cands = [{"id": 7, "title": "EDR setup – Shreya", "project": "MSOC", "status": "open"}]
    d = clean_draft({"summary": "x", "date": "2026-10-07", "items": [
        {"match": 1, "status": "done", "note": "Finished EDR", "due": "nope"},
        {"match": None, "title": "Fix CRM blueprint", "tasklist": "Made Up", "status": "weird"},
        {"match": 9, "title": ""}, "junk"]}, cands, lists, "2026-10-07")
    assert len(d["items"]) == 2
    a, b = d["items"]
    assert (a["task_id"], a["title"], a["was"], a["status"], a["due"]) == (7, "EDR setup – Shreya", "open", "done", None)
    assert (b["task_id"], b["tasklist"], b["status"]) == (None, "General ++", "open")
    assert existing({"zoho_id": "z1"}) and not existing(b)
    text = format_draft(d)
    assert "Open → <b>Done</b>" in text and "NEW in General ++" in text
    assert valid_date("2026-13-40", "f") == "f"
    assert '"action": "redate"' in build_prompt("x", cands, lists)
    assert "Zoho (never Jovo" in build_prompt("x", cands, lists)
    assert fix_names("Hi Mambo, the jovo CRM with Suyas") == "Hi Mumbo, the Zoho CRM with Suyash"
    print("self-check ok")
