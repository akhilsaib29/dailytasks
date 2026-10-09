-- Run once in Supabase → SQL Editor (fresh database).

create table projects (            -- one row per task list
  name        text primary key,
  area        text not null default 'General',
  description text default '',
  created_at  timestamptz default now()
);

create table tasks (               
  id           bigint generated always as identity primary key,
  zoho_id      text unique,
  title        text not null,
  description  text default '',
  project      text not null references projects(name) on update cascade,
  date         date,  -- due date; null = backlog
  priority     text not null default 'med' check (priority in ('high','med','low')),
  status       text not null default 'open' check (status in ('open','in_progress','in_review','on_hold','done','cancelled')),
  done         boolean not null default false,
  completed_at date,
  created_at   timestamptz default now()
);

create table drafts (              -- every Telegram message; Proceed flips status to 'saved'
  id          bigint generated always as identity primary key,
  chat_id     bigint not null,
  source      text not null,
  draft       jsonb not null,
  status      text not null default 'pending' check (status in ('pending','editing','saved','cancelled')),
  created_at  timestamptz default now()
);

create table worklog (             -- what you worked on, per day (the ED report)
  id          bigint generated always as identity primary key,
  date        date not null,
  zoho_id     text,
  title       text not null,
  tasklist    text,
  note        text default '',
  status      text,
  draft_id    bigint,
  created_at  timestamptz default now()
);

create table kv (                  -- unused since Zoho was dropped
  key        text primary key,
  value      jsonb,
  updated_at timestamptz default now()
);

-- RLS on with no policies: only the server's service_role key can read/write.
alter table projects enable row level security;
alter table tasks    enable row level security;
alter table drafts   enable row level security;
alter table worklog  enable row level security;
alter table kv       enable row level security;


-- ===== Upgrade an existing (pre-Zoho) database instead: run only this block =====
-- alter table tasks add column if not exists completed_at date;
-- alter table tasks add column if not exists zoho_id text unique;
-- create table if not exists worklog (id bigint generated always as identity primary key, date date not null,
--   zoho_id text, title text not null, tasklist text, note text default '', status text, draft_id bigint,
--   created_at timestamptz default now());
-- create table if not exists kv (key text primary key, value jsonb, updated_at timestamptz default now());
-- alter table worklog enable row level security;
-- alter table kv enable row level security;
