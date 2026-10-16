# Visitor log (Supabase table)

The dashboard sends every sign-in and the first visit of each browser session to a Supabase table through its REST API. Supabase gives a real table with a viewer, filters and CSV export. The free plan is enough.

1. Sign up at supabase.com (GitHub login works) and create a project.
2. SQL Editor > New query: paste [supabase.sql](supabase.sql) > Run.
3. Project Settings > API: copy the **Project URL** and the **anon / publishable key**.
4. In `docs/index.html` set `LOG_URL` (project URL) and `LOG_KEY` (anon key), then commit and push.

Where to look

| Place | Content |
|---|---|
| Table Editor > `visits` | one row per event (`signin` or `visit`): time, e-mail, language, user agent |
| Table Editor > `visitors_summary` | one row per e-mail: first seen, last seen, events, sign-ins |

Export: Table Editor > Export > CSV (opens in Excel).

Notes
- The anon key is public by design. Row-level security and `revoke` in the SQL let it **insert only**; it cannot read, edit or delete anything. Do not use the `service_role` key in the page.
- The table rejects any e-mail outside the four company domains, even if someone posts to the API directly.
- The e-mail gate is a client-side "soft gate": addresses are not verified and the published data files stay publicly reachable. It records and discourages; it does not protect. Someone who types a fake `@xm.com` address is logged under that address.
- Changing the allowed domains: edit `ALLOWED_DOMAINS` in `docs/index.html` and the check constraint in `supabase.sql`.
- Employee e-mails are stored on a third-party service. Confirm this is acceptable under company policy first.
