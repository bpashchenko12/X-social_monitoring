# Visitor log (Google Sheet)

The dashboard sends every sign-in / first visit of a session to a small Google Apps Script web app, which writes to a Google Sheet.

1. Create a new Google Sheet (e.g. "XM Monitor visitors").
2. Extensions > Apps Script. Replace the editor content with [Code.gs](Code.gs). Save.
3. Deploy > New deployment > type **Web app**.
   - Execute as: **Me**
   - Who has access: **Anyone**
4. Authorize when asked, then copy the **Web app URL** (ends with `/exec`).
5. In `docs/index.html` set `const LOG_URL = "<the URL>";`, commit and push.

Sheets created automatically:

| Sheet | Content |
|---|---|
| Visitors | one row per e-mail: domain, first seen, last seen, visit count, last user agent, language |
| Log | one row per event (`signin` or `visit`) |

Notes
- The e-mail gate is a client-side "soft gate": addresses are not verified and the published data files stay publicly reachable. It records and discourages, it does not protect.
- The Apps Script re-checks the domain list, so fake non-company addresses sent straight to the URL are rejected. Someone who types a fake `@xm.com` address is still logged under that address.
- Changing the allowed domains: edit `ALLOWED_DOMAINS` in both `docs/index.html` and `Code.gs` (after editing the script, create a new deployment version).
