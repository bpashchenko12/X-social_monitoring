# Local scheduled refresh (macOS launchd)

Used while the GitHub Actions schedule is switched off. Two LaunchAgents run `tools/local_refresh.sh`
(scrape, quality gates, commit and push of the data) in the Mac's local time:

| Agent | Local time | Scope |
|---|---|---|
| `com.xmmonitor.refresh.all` | 09:30 | XM + competitors + Industry |
| `com.xmmonitor.refresh.xm`  | 17:30 | XM only |

A run missed while the Mac sleeps is executed on wake-up. LLM translation is off, so new posts stay
"Translation pending" until translated in the IDE; a macOS notification tells you when posts wait.
Logs: `~/Library/Logs/xm-monitor-refresh.log`. Spend limits (Apify $50/month) are enforced by the pipeline.

Install / remove:

    cp tools/launchd/*.plist ~/Library/LaunchAgents/
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.xmmonitor.refresh.all.plist
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.xmmonitor.refresh.xm.plist

    launchctl bootout gui/$(id -u)/com.xmmonitor.refresh.all
    launchctl bootout gui/$(id -u)/com.xmmonitor.refresh.xm
    rm ~/Library/LaunchAgents/com.xmmonitor.refresh.*.plist

Run once by hand: `tools/local_refresh.sh all` (or `xm`).
