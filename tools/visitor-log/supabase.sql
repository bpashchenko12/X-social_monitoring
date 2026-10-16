-- Visitor log for the XM X-mention dashboard. Run once in Supabase: SQL Editor > New query > Run.

create table if not exists public.visits (
  id         bigint generated always as identity primary key,
  created_at timestamptz not null default now(),
  email      text not null check (
               length(email) <= 254
               and lower(email) ~ '^[a-z0-9._%+''-]+@(xmtrading\.com|xm\.com|trading-point\.com|tradingpoint\.com)$'),
  kind       text not null check (kind in ('signin', 'visit')),
  lang       text check (length(lang) <= 8),
  ua         text check (length(ua) <= 200),
  page       text check (length(page) <= 200)
);

-- The public (anon) key may INSERT only. It can never read, change or delete rows.
alter table public.visits enable row level security;
revoke all on public.visits from anon, authenticated;
grant insert on public.visits to anon;
drop policy if exists "anon can insert" on public.visits;
create policy "anon can insert" on public.visits for insert to anon with check (true);

-- One row per person (open it in Table Editor > visitors_summary).
create or replace view public.visitors_summary with (security_invoker = true) as
  select lower(email)                       as email,
         split_part(lower(email), '@', 2)   as domain,
         min(created_at)                    as first_seen,
         max(created_at)                    as last_seen,
         count(*)                           as events,
         count(*) filter (where kind = 'signin') as signins
  from public.visits
  group by lower(email)
  order by max(created_at) desc;
revoke all on public.visitors_summary from anon, authenticated;
