"""Mumbo: Telegram voice/text -> draft -> Proceed -> Zoho Projects tasks + dated work log -> dashboard.

Zoho Projects (one project, ZOHO_PROJECT_ID) is the source of truth for tasks; Supabase mirrors it
(`tasks`, `projects` = Zoho task lists) and owns the work log (`worklog`) and drafts.
"""
import json
import os
import re
import time
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
ZP = f"https://projectsapi.zoho.in/api/v3/portal/{E('ZOHO_PORTAL_ID')}/projects/{E('ZOHO_PROJECT_ID')}"
ZOHO_ACCOUNTS = "https://accounts.zoho.in/oauth/v2/token"
IST = timezone(timedelta(hours=5, minutes=30))
SYNC_EVERY = 300  # seconds; dashboard loads and new messages pull Zoho at most this often

# our status <-> Zoho status name (lowercase). Zoho ids are learned from synced tasks.
TO_ZOHO = {"open": "open", "in_progress": "in progress", "in_review": "in review",
           "on_hold": "on hold", "done": "closed", "cancelled": "cancelled"}
FROM_ZOHO = {v: k for k, v in TO_ZOHO.items()}
PRIORITY_TO_ZOHO = {"high": "high", "med": "medium", "low": "low"}
PRIORITY_FROM_ZOHO = {"high": "high", "medium": "med", "low": "low", "none": "med"}
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


def kv_get(key):
    rows = sb("GET", f"kv?key=eq.{key}")
    return rows[0]["value"] if rows else None


def kv_set(key, value):
    sb("POST", "kv?on_conflict=key", {"key": key, "value": value}, prefer="resolution=merge-duplicates,return=minimal")


def tg(method, **params):
    return http.post(f"{TG}/{method}", json=params).json()


def send(chat, text, buttons=None):
    # ponytail: hard cut at 4000 chars can split an HTML tag; fine for ~10 items per message
    p = {"chat_id": chat, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": True}
    if buttons:
        p["reply_markup"] = {"inline_keyboard": buttons}
    return tg("sendMessage", **p)


def groq_auth():
    return {"Authorization": f"Bearer {E('GROQ_API_KEY')}"}


def transcribe(data, filename):
    r = http.post(f"{GROQ}/audio/transcriptions", headers=groq_auth(), files={"file": (filename, data)},
                  data={"model": E("WHISPER_MODEL", "whisper-large-v3"), "response_format": "json"})
    if r.is_error:
        raise RuntimeError(f"Groq {r.status_code}: {r.text[:300]}")
    return r.json()["text"].strip()


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


# ---------- Zoho Projects ----------
def zoho_token(force=False):
    """Access tokens live 1h; cache in Supabase so serverless cold starts don't hit Zoho's token rate limit."""
    cached = None if force else kv_get("zoho_token")
    if cached and cached["exp"] > time.time() + 60:
        return cached["token"]
    r = http.post(ZOHO_ACCOUNTS, params={"refresh_token": E("ZOHO_REFRESH_TOKEN"), "client_id": E("ZOHO_CLIENT_ID"),
                                         "client_secret": E("ZOHO_CLIENT_SECRET"), "grant_type": "refresh_token"})
    j = r.json()
    if "access_token" not in j:
        raise RuntimeError(f"Zoho login failed: {j.get('error', j)}")
    kv_set("zoho_token", {"token": j["access_token"], "exp": time.time() + int(j.get("expires_in", 3600))})
    return j["access_token"]


def zoho(method, path, body=None, params=None):
    for attempt in (0, 1):
        r = http.request(method, f"{ZP}/{path}" if path else ZP, json=body, params=params,
                         headers={"Authorization": f"Zoho-oauthtoken {zoho_token(force=attempt == 1)}"})
        if r.status_code != 401:
            break
    if r.is_error:
        raise RuntimeError(f"Zoho {r.status_code}: {r.text[:300]}")
    return r.json() if r.content else {}


def zoho_write(method, path, body):
    """Send a due date as an IST datetime; portals in date-only mode reject that, so retry as plain date."""
    try:
        return zoho(method, path, body)
    except RuntimeError as e:
        if "end_date" not in body or "400" not in str(e):
            raise
        return zoho(method, path, {**body, "end_date": body["end_date"][:10]})


def zoho_date(d):
    return f"{d}T18:00:00+05:30"


def ist_date(s):
    if not s:
        return None
    if len(s) == 10:
        return s
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(IST).date().isoformat()


def task_row(z):
    status = FROM_ZOHO.get(z["status"]["name"].strip().lower(), "open")
    return {
        "zoho_id": z["id"], "title": z["name"].strip()[:200], "project": z["tasklist"]["name"].strip(),
        "status": status, "done": status in ("done", "cancelled"),
        "priority": PRIORITY_FROM_ZOHO.get(z.get("priority") or "none", "med"),
        "date": ist_date(z.get("end_date")), "completed_at": ist_date(z.get("completed_on")),
        "created_at": z.get("created_time"),
        **({"description": re.sub(r"<[^>]+>", " ", z["description"]).strip()[:2000]} if z.get("description") else {}),
    }


def sync(force=False):
    """Pull every task list and task from Zoho into Supabase. Cheap: ~2 API calls for ~150 tasks."""
    last = kv_get("last_sync") or 0
    if not force and time.time() - last < SYNC_EVERY:
        return
    lists = zoho("GET", "tasklists")["tasklists"]
    tasks, page = [], 1
    while True:
        d = zoho("GET", "tasks", params={"page": page, "per_page": 200})
        tasks += d.get("tasks", [])
        if not d.get("page_info", {}).get("has_next_page"):
            break
        page += 1
    statuses = {t["status"]["name"].strip().lower(): t["status"]["id"] for t in tasks}
    kv_set("zoho_meta", {"lists": {l["name"].strip(): l["id"] for l in lists},
                         "statuses": {**(kv_get("zoho_meta") or {}).get("statuses", {}), **statuses},
                         "tags": zoho_tags()})
    sb("POST", "projects?on_conflict=name", [{"name": l["name"].strip(), "area": "IT Tasks"} for l in lists],
       prefer="resolution=merge-duplicates,return=minimal")
    rows = [task_row(t) for t in tasks]
    for keys in {tuple(sorted(r)) for r in rows}:  # PostgREST bulk upsert needs identical keys per batch
        batch = [r for r in rows if tuple(sorted(r)) == keys]
        sb("POST", "tasks?on_conflict=zoho_id", batch, prefer="resolution=merge-duplicates,return=minimal")
    ids = ",".join(t["id"] for t in tasks)
    if ids:  # tasks deleted in Zoho disappear locally too
        sb("DELETE", f"tasks?zoho_id=not.in.({ids})", prefer="return=minimal")
    kv_set("last_sync", time.time())


def zoho_tags():
    r = http.get(f"https://projectsapi.zoho.in/api/v3/portal/{E('ZOHO_PORTAL_ID')}/tags", params={"per_page": 200},
                 headers={"Authorization": f"Zoho-oauthtoken {zoho_token()}"})
    return {t["name"]: t["id"] for t in r.json().get("tags", [])} if r.is_success else {}


def status_body(status, meta):
    sid = meta["statuses"].get(TO_ZOHO[status])
    if not sid:
        raise RuntimeError(f"Zoho has no '{TO_ZOHO[status]}' status in this project yet")
    return {"status": {"id": sid}}


# ---------- drafting ----------
PROMPT = """You turn a person's spoken or typed work update into a work log against their Zoho Projects tasks.
The notes can be in any language or a mix (Hindi, Hinglish, Marathi, Tamil, English, ...).
Today is {today} ({weekday}), India time. Weeks start Monday.

Existing tasks (number | task list | status | title):
{tasks}

Task lists: {lists}
Allowed tags: {tags}

Rules:
- Make one item per piece of work mentioned. Split compound sentences.
- If an item is the same work as an existing task, set "match" to that task's number. Otherwise "match": null and write a new task: title (starts with a verb, under 80 chars), description (2-3 sentences of useful context from the notes, never invent facts), tasklist (exactly one of the task lists), tags (0-3 from the allowed tags, only when clearly relevant).
- note: one sentence in past tense of what the person did or said about it, for their daily work log.
- status after this update: "done" if finished, "in_progress" if worked on and not finished, "in_review" if waiting on review/approval, "on_hold" if paused/blocked, "open" for new work not started.
- due: YYYY-MM-DD only if a deadline is stated (resolve "Friday", "next week" = next Monday), else null.
- priority: "high" if urgent, "low" if it can wait, else "med".
- date: the day the work happened, YYYY-MM-DD (today unless they say yesterday or another day).
- Write titles, descriptions, notes and the summary in {language}.
{revision}
EXCEPTION: if the message is not a work update but asks to change the DATE of their previous update
(e.g. "change that to yesterday", "move my last update to Monday", "that was for 5th October"),
reply only {{"action": "redate", "date": "YYYY-MM-DD"}} with the new date resolved.

Otherwise reply with only a JSON object:
{{"summary": "one sentence", "date": "YYYY-MM-DD", "items": [{{"match": 12, "title": "", "description": "", "tasklist": "", "tags": [], "status": "", "priority": "med", "due": null, "note": ""}}]}}

Notes:
\"\"\"
{notes}
\"\"\""""


def today():
    return datetime.now(IST).date()


def candidates():
    """Open tasks plus anything closed in the last 30 days: what a new update could refer to."""
    since = (today() - timedelta(days=30)).isoformat()
    return sb("GET", f"tasks?zoho_id=not.is.null&or=(done.eq.false,completed_at.gte.{since})"
                     "&select=zoho_id,title,project,status&order=project,title")


def build_prompt(notes, cands, meta, current=None):
    revision = ("This is a REVISION. Current draft:\n" + json.dumps(current, ensure_ascii=False) +
                "\nApply the corrections in the notes below and return the full updated draft.") if current else ""
    t = today()
    listing = "\n".join(f"{i} | {c['project']} | {LABEL[c['status']]} | {c['title']}" for i, c in enumerate(cands, 1))
    return PROMPT.format(today=t.isoformat(), weekday=t.strftime("%A"), tasks=listing or "none",
                         lists=", ".join(meta["lists"]), tags=", ".join(meta["tags"]) or "none",
                         language=E("OUTPUT_LANGUAGE", "English"), revision=revision, notes=notes[:20000])


def valid_date(s, fallback):
    try:
        return datetime.strptime(str(s), "%Y-%m-%d").date().isoformat()
    except ValueError:
        return fallback


def clean_draft(d, cands, meta, fallback_date):
    """Normalize whatever the LLM returned into a safe draft: matches must be real tasks, lists and tags must exist."""
    lists = list(meta["lists"])
    default_list = next((l for l in lists if l.lower().startswith("general")), lists[0] if lists else "")
    items = []
    for it in d.get("items") or []:
        if not isinstance(it, dict):
            continue
        m = it.get("match")
        c = cands[m - 1] if isinstance(m, int) and 1 <= m <= len(cands) else None
        if not c and not str(it.get("title") or "").strip():
            continue
        status = it.get("status") if it.get("status") in TO_ZOHO else ("in_progress" if c else "open")
        item = {"status": status, "note": str(it.get("note") or "").strip()[:500],
                "due": valid_date(it.get("due"), None),
                "priority": it.get("priority") if it.get("priority") in PRIORITY_TO_ZOHO else "med"}
        if c:
            item.update(zoho_id=c["zoho_id"], title=c["title"], tasklist=c["project"], was=c["status"])
        else:
            tl = str(it.get("tasklist") or "").strip()
            item.update(zoho_id=None, title=str(it["title"]).strip()[:200],
                        description=str(it.get("description") or "").strip()[:2000],
                        tasklist=tl if tl in meta["lists"] else default_list,
                        tags=[t for t in dict.fromkeys(it.get("tags") or []) if t in meta["tags"]][:3])
        items.append(item)
    return {"summary": str(d.get("summary") or "").strip()[:300], "date": valid_date(d.get("date"), fallback_date),
            "items": items}


def format_draft(d):
    day = datetime.strptime(d["date"], "%Y-%m-%d").strftime("%a %d %b")
    out = [f"📅 <b>{day}</b>: {escape(d['summary'])}", ""]
    for it in d["items"]:
        due = f" · due {datetime.strptime(it['due'], '%Y-%m-%d').strftime('%d %b')}" if it.get("due") else ""
        if it["zoho_id"]:
            change = f"{LABEL[it['was']]} → <b>{LABEL[it['status']]}</b>" if it["status"] != it["was"] else LABEL[it["status"]]
            out.append(f"{MARK[it['status']]} <b>{escape(it['title'])}</b>\n     existing · {escape(it['tasklist'])} · {change}{due}")
        else:
            tags = f" · 🏷 {escape(', '.join(it['tags']))}" if it.get("tags") else ""
            out.append(f"➕ <b>{escape(it['title'])}</b>\n     NEW in {escape(it['tasklist'])} · {LABEL[it['status']]}{tags}{due}")
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


def apply_item(it, d, meta):
    """Write one draft item to Zoho. Returns the Zoho task id. Each item is marked done in the draft as it lands,
    so a retry after a partial failure never creates the same task twice."""
    body = {}
    if it["zoho_id"]:
        tid = it["zoho_id"]
        if it["status"] != it["was"]:
            body.update(status_body(it["status"], meta))
        if it.get("due"):
            body["end_date"] = zoho_date(it["due"])
        if body:
            zoho_write("PATCH", f"tasks/{tid}", body)
    else:
        body = {"name": it["title"], "description": it.get("description", ""),
                "tasklist": {"id": meta["lists"][it["tasklist"]]},
                "priority": PRIORITY_TO_ZOHO[it["priority"]],
                "owners_and_work": {"owners": [{"zpuid": E("ZOHO_OWNER_ZPUID")}]},
                "tags": [{"id": meta["tags"][t]} for t in it.get("tags", [])]}
        if it["status"] != "open":
            body.update(status_body(it["status"], meta))
        if it.get("due"):
            body["end_date"] = zoho_date(it["due"])
        res = zoho_write("POST", "tasks", body)
        tid = (res.get("tasks") or res.get("result") or [res])[0]["id"] if not res.get("id") else res["id"]
    if it["note"]:
        res = zoho("POST", f"tasks/{tid}/comments", {"comment": f"{d['date']}: {it['note']} (via Mumbo)"})
        it["comment_id"] = ((res.get("result") or res.get("comments") or [{}])[0]).get("id")
    return tid


def redate_comment(tid, cid, old, new):
    """Swap the 'YYYY-MM-DD:' prefix on Mumbo's comment. Older items didn't store the comment id: find it."""
    if not cid:
        found = [c for c in zoho("GET", f"tasks/{tid}/comments").get("comments", [])
                 if c["comment"].startswith(f"{old}:") and c["comment"].endswith("(via Mumbo)")]
        if not found:
            return
        cid, text = found[0]["id"], found[0]["comment"]
    else:
        text = next((c["comment"] for c in zoho("GET", f"tasks/{tid}/comments").get("comments", []) if c["id"] == cid), "")
        if not text.startswith(f"{old}:"):
            return
    zoho("PATCH", f"tasks/{tid}/comments/{cid}", {"comment": new + text[len(old):]})


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
                          "what you worked on. Commands: /report [today|yesterday|week|lastweek|date] /today /week /sync")
    if not allowed(chat):
        return
    if text.startswith("/report"):
        return send(chat, report(text[7:]))
    if text.startswith("/today"):
        return send(chat, task_list(today().isoformat(), "Due today + overdue"))
    if text.startswith("/week"):
        t = today()
        return send(chat, task_list((t + timedelta(days=6 - t.weekday())).isoformat(), "Due this week + overdue"))
    if text.startswith("/sync"):
        sync(force=True)
        return send(chat, "🔄 Synced with Zoho Projects.")

    media = msg.get("voice") or msg.get("audio") or msg.get("video_note")
    if media:
        tg("sendChatAction", chat_id=chat, action="typing")
        path = tg("getFile", file_id=media["file_id"])["result"]["file_path"]
        text = transcribe(http.get(f"{TG_FILE}/{path}").content, Path(path).name.replace(".oga", ".ogg"))
        send(chat, f"🎙 <i>{escape(text[:1500])}</i>")
    if not text:
        return send(chat, "Send me a voice note or a text message.")

    tg("sendChatAction", chat_id=chat, action="typing")
    sync()
    meta, cands = kv_get("zoho_meta"), candidates()
    editing = sb("GET", f"drafts?chat_id=eq.{chat}&status=eq.editing&order=id.desc&limit=1")
    if editing:
        old = editing[0]
        draft = clean_draft(ask_llm(build_prompt(text, cands, meta, old["draft"])), cands, meta, today().isoformat())
        row = sb("PATCH", f"drafts?id=eq.{old['id']}",
                 {"draft": draft, "status": "pending", "source": old["source"] + "\n\nCorrection: " + text})[0]
    else:
        raw = ask_llm(build_prompt(text, cands, meta))
        if raw.get("action") == "redate":
            return offer_redate(chat, text, valid_date(raw.get("date"), None))
        draft = clean_draft(raw, cands, meta, today().isoformat())
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
    lines += ["", "This changes its work log date and Mumbo's dated comments in Zoho."]
    send(chat, "\n".join(lines), [[{"text": "✅ Proceed", "callback_data": f"p:{row['id']}"},
                                    {"text": "❌ Cancel", "callback_data": f"c:{row['id']}"}]])


def apply_redate(chat, d):
    target = sb("GET", f"drafts?id=eq.{d['target']}")[0]
    old, new, failed = d["from"], d["date"], []
    for it in target["draft"]["items"]:
        if it.get("applied") and it.get("note"):
            try:
                redate_comment(it["applied"], it.get("comment_id"), old, new)
            except Exception as e:
                failed.append(f"{it['title']}: {str(e)[:120]}")
    sb("PATCH", f"worklog?draft_id=eq.{d['target']}", {"date": new}, prefer="return=minimal")
    sb("PATCH", f"drafts?id=eq.{d['target']}", {"draft": {**target["draft"], "date": new}}, prefer="return=minimal")
    msg = f"✅ Moved to {datetime.strptime(new, '%Y-%m-%d'):%a %d %b}: work log and Zoho comments."
    if failed:
        msg += "\n⚠️ Zoho comments not changed:\n" + "\n".join(f"• {escape(f)}" for f in failed)
    send(chat, msg)


def proceed(chat, draft_id, d):
    meta, done, failed = kv_get("zoho_meta"), [], []
    for it in d["items"]:
        if it.get("applied"):
            continue
        try:
            tid = apply_item(it, d, meta)
            sb("POST", "worklog", {"date": d["date"], "zoho_id": tid, "title": it["title"], "tasklist": it["tasklist"],
                                   "note": it["note"], "status": it["status"], "draft_id": draft_id},
               prefer="return=minimal")
            it["applied"] = tid
            done.append(it)
        except Exception as e:
            failed.append((it, str(e)))
        sb("PATCH", f"drafts?id=eq.{draft_id}", {"draft": d}, prefer="return=minimal")  # progress survives a crash
    sync(force=True)
    lines = [f"{'➕' if not it['zoho_id'] else MARK[it['status']]} {escape(it['title'])}" for it in done]
    if failed:
        sb("PATCH", f"drafts?id=eq.{draft_id}", {"status": "pending"}, prefer="return=minimal")
        lines += ["", "⚠️ <b>Not saved yet</b> (tap Proceed to retry only these):"]
        lines += [f"• {escape(it['title'])}: {escape(err[:150])}" for it, err in failed]
        return send(chat, "\n".join(lines), buttons(draft_id))
    send(chat, f"✅ Saved to Zoho and your work log for {d['date']}:\n" + "\n".join(lines))


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


def zoho_ready():
    return all(E(k) for k in ("ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_REFRESH_TOKEN", "ZOHO_PORTAL_ID", "ZOHO_PROJECT_ID"))


@app.get("/api/data")
def data(x_key: str = Header("")):
    check(x_key)
    sync_error = None
    if zoho_ready():
        try:
            sync()
        except Exception as e:  # show stale data rather than nothing
            sync_error = str(e)[:300]
    logs = sb("GET", "drafts?status=eq.saved&select=source,draft,created_at&order=id.desc&limit=300")
    since = (today() - timedelta(days=180)).isoformat()
    return {"tasks": sb("GET", "tasks?order=date"), "projects": sb("GET", "projects"),
            "worklog": sb("GET", f"worklog?date=gte.{since}&order=date.desc,id"),
            "logs": [{"summary": l["draft"].get("summary", ""),
                      "count": len(l["draft"].get("items") or l["draft"].get("tasks") or []),
                      "text": l["source"], "created_at": l["created_at"]} for l in logs],
            "last_sync": kv_get("last_sync") if zoho_ready() else None, "sync_error": sync_error}


@app.post("/api/sync")
def sync_now(x_key: str = Header("")):
    check(x_key)
    sync(force=True)
    return {"ok": True}


@app.patch("/api/tasks/{tid}")
def update_task(tid: int, body: dict, x_key: str = Header("")):
    check(x_key)
    patch = {k: body[k] for k in ("title", "description", "status", "priority", "date") if k in body}
    if "date" in patch:
        patch["date"] = valid_date(patch["date"], None)
    if not patch:
        return {"ok": True}
    row = sb("GET", f"tasks?id=eq.{tid}&select=zoho_id,done")[0]
    if row.get("zoho_id"):  # Zoho first: if it refuses, nothing changes locally either
        z = {}
        if "title" in patch:
            z["name"] = patch["title"]
        if "description" in patch:
            z["description"] = patch["description"]
        if "priority" in patch:
            z["priority"] = PRIORITY_TO_ZOHO.get(patch["priority"], "medium")
        if "status" in patch:
            z.update(status_body(patch["status"], kv_get("zoho_meta")))
        if patch.get("date"):
            z["end_date"] = zoho_date(patch["date"])
        if z:
            zoho_write("PATCH", f"tasks/{row['zoho_id']}", z)
    if "status" in patch:
        patch["done"] = patch["status"] in ("done", "cancelled")
        if row["done"] != patch["done"]:
            patch["completed_at"] = today().isoformat() if patch["done"] else None
    sb("PATCH", f"tasks?id=eq.{tid}", patch, prefer="return=minimal")
    return {"ok": True}


@app.delete("/api/tasks/{tid}")
def delete_task(tid: int, x_key: str = Header("")):
    check(x_key)
    row = sb("GET", f"tasks?id=eq.{tid}&select=zoho_id")[0]
    if row.get("zoho_id"):
        zoho("DELETE", f"tasks/{row['zoho_id']}")
    sb("DELETE", f"tasks?id=eq.{tid}", prefer="return=minimal")
    return {"ok": True}


@app.get("/api/setup")
def setup(key: str, request: Request):
    """Open once after deploy: points the Telegram bot at this server."""
    check(key)
    url = E("PUBLIC_URL") or f"https://{request.headers['host']}"
    return tg("setWebhook", url=f"{url}/api/telegram", secret_token=E("TELEGRAM_SECRET"),
              allowed_updates=["message", "callback_query"])


@app.get("/api/zoho/connect")
def zoho_connect(key: str, code: str):
    """One-time: swap the Self Client grant code for a refresh token to paste into Vercel as ZOHO_REFRESH_TOKEN."""
    check(key)
    j = http.post(ZOHO_ACCOUNTS, params={"code": code.strip(), "client_id": E("ZOHO_CLIENT_ID"),
                                         "client_secret": E("ZOHO_CLIENT_SECRET"), "grant_type": "authorization_code"}).json()
    if "refresh_token" not in j:
        return {"ok": False, "error": j.get("error", j),
                "hint": "Codes expire after the minutes you chose; generate a new one and open this link again quickly."}
    return {"ok": True, "ZOHO_REFRESH_TOKEN": j["refresh_token"], "next": "Add it in Vercel, then redeploy."}


@app.middleware("http")
async def vercel_path(request: Request, call_next):
    # Vercel's rewrite hands every call over as /api/index; vercel.json passes the real path in ?__path=
    real = request.query_params.get("__path")
    if real is not None and request.url.path == "/api/index":
        request.scope["path"] = "/api/" + real
    return await call_next(request)


@app.exception_handler(RuntimeError)
def upstream_error(request: Request, exc):  # Database/Groq/Zoho errors reach the dashboard with their real message
    return JSONResponse({"detail": str(exc)}, status_code=502)


@app.exception_handler(404)
def not_found(request: Request, exc):
    return JSONResponse({"detail": "Not Found", "path": request.url.path}, status_code=404)


@app.get("/")
def home():  # Vercel serves index.html itself; this is for local runs
    return FileResponse(Path(__file__).parent.parent / "index.html")


if __name__ == "__main__":
    meta = {"lists": {"MSOC": "1", "General ++": "2"}, "tags": {"CRM": "t1", "People": "t2"},
            "statuses": {"open": "s1", "in progress": "s2", "closed": "s3"}}
    cands = [{"zoho_id": "z1", "title": "EDR setup – Shreya", "project": "MSOC", "status": "open"}]
    d = clean_draft({"summary": "x", "date": "2026-10-07", "items": [
        {"match": 1, "status": "done", "note": "Finished EDR", "due": "nope"},
        {"match": None, "title": "Fix CRM blueprint", "tasklist": "Made Up", "tags": ["CRM", "Invented"], "status": "weird"},
        {"match": 9, "title": ""}, "junk"]}, cands, meta, "2026-10-07")
    assert len(d["items"]) == 2
    a, b = d["items"]
    assert (a["zoho_id"], a["title"], a["was"], a["status"], a["due"]) == ("z1", "EDR setup – Shreya", "open", "done", None)
    assert (b["zoho_id"], b["tasklist"], b["tags"], b["status"]) == (None, "General ++", ["CRM"], "open")
    assert status_body("done", meta) == {"status": {"id": "s3"}}
    text = format_draft(d)
    assert "Open → <b>Done</b>" in text and "NEW in General ++" in text
    z = task_row({"id": "9", "name": " RAG ", "tasklist": {"name": "Sales flow / Compliance "},
                  "status": {"name": "In Progress"}, "priority": "medium", "end_date": "2026-10-10T18:30:00Z",
                  "created_time": "2026-06-03T10:43:18.899Z"})
    assert (z["title"], z["project"], z["status"], z["priority"], z["date"]) == ("RAG", "Sales flow / Compliance", "in_progress", "med", "2026-10-11")
    assert valid_date("2026-13-40", "f") == "f"
    assert '"action": "redate"' in build_prompt("x", cands, meta)
    print("self-check ok")
