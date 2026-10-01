-- Run once in Supabase → SQL Editor.
create table projects (
  name        text primary key,
  area        text not null default 'General',  -- area > project > task
  description text default '',
  created_at  timestamptz default now()
);

create table tasks (
  id          bigint generated always as identity primary key,
  title       text not null,
  description text default '',
  project     text not null references projects(name) on update cascade,
  date        date,  -- null = backlog (no date yet)
  priority    text not null default 'med' check (priority in ('high','med','low')),
  status      text not null default 'open' check (status in ('open','in_progress','on_hold','in_review','done','cancelled')),
  done        boolean not null default false,
  created_at  timestamptz default now()
);

-- Every message you send becomes a draft; Proceed flips status to 'saved'.
create table drafts (
  id          bigint generated always as identity primary key,
  chat_id     bigint not null,
  source      text not null,
  draft       jsonb not null,
  status      text not null default 'pending' check (status in ('pending','editing','saved','cancelled')),
  created_at  timestamptz default now()
);

-- RLS on with no policies: only the server's service_role key can read/write.
alter table projects enable row level security;
alter table tasks    enable row level security;
alter table drafts   enable row level security;
