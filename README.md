# Entry

A private, minimal control panel for Ring Intercom Video auto-entry. The running
integration polls active buzzes every two seconds and unlocks the selected entrance
when a fresh buzz falls inside an enabled time window. No answering, voice or video
is required for auto-entry. Ring session tokens stay in persistent Fly storage.

## Controls

- Main switch: turn on for 15 minutes, 1 hour, 4 hours, or until a chosen time.
- Turning off pauses all saved windows and clears the manual timer.
- Resume saved windows without deleting or recreating them.
- One-off windows: choose exact start/end dates and times.
- Weekly windows: choose weekdays and clock times, including overnight windows.
- Disable or remove individual windows.
- London timezone by default; weekly windows follow local clock changes.
- Current state, next opening/closing time, Ring connection and recent entries.

Anyone who buzzes during an active window can enter. Windows are start-inclusive
and end-exclusive. Overlapping windows combine, so entry stays on until the last
one ends. Pausing overrides every window. Chosen windows and unexpired manual
timers survive ordinary redeploys. Unlocking is rechecked against the current
settings immediately before the request. Requests are not automatically retried.

## Google sign-in

The implementation uses Google Identity Services and the maintained `google-auth`
library for ID-token verification. It needs a Google Web application client ID:

1. In Google Cloud, create/select a project and open Google Auth Platform → Clients.
2. Create a Web application client.
3. Add the app's actual Fly origin and `https://entry.damonfalck.me` to Authorized
   JavaScript origins. Use the actual allocated Fly name, not a proposed name.
4. In Entry → Settings → Google sign-in, enter that public client ID and the one
   Google email permitted to access the app. No client secret is used or needed.
5. Use the Google button in Settings once to link the account.

Signature, issuer, audience and expiration are checked by Google's library. The
app also checks verified email, nonce and stable Google subject. After linking,
the old setup key is rejected for browser login and admin API access. Existing
setup sessions are revoked. Browser sessions use Secure, HttpOnly, host-only
cookies, last 30 days, and persist in SQLite across restarts. Changing settings
with a cookie session also requires the per-session CSRF token.

Before Google is linked, the existing setup key can bootstrap access. The old
per-tab key is migrated to an opaque cookie when opening an updated app on the
same host. The new Fly hostname may require this one-time setup again unless
Google has already been configured for that origin.

Google sign-in cannot be verified live until a real client ID and account are
configured. Local tests stub the provider response to check the app's restrictions;
they do not claim to test Google's live popup/consent process.

## Hostnames and DNS

Allocated short Fly name: `damon-entry.fly.dev`. Migration is prepared; confirm
the deployment workflow succeeds before treating the new address as active.
Custom hostname: `entry.damonfalck.me`.

Fly apps cannot be renamed in place. The prepared migration creates a new app,
copies the existing Ring session and entry history securely within the same Fly
account, verifies its Ring connection with entry paused, then stops the old entry
worker before activating the new one. The old address becomes a small redirect
service that can stop when idle. Its original volume is retained for recovery.
Only one entry worker is active at cutover.

## Deployment from GitHub

The **Deploy Entry** workflow is manual. It completes the already prepared Fly
migration on its first run and updates the existing new machine on later runs.
It preserves the persistent volume, Ring session, browser sessions and windows.
It replaces the old worker with a redirect and confirms that redirect is serving
before restoring entry settings on the new app. Progress is checkpointed on the
new volume so a failed migration can be resumed. The first run requires the new
worker to be paused and connected to Ring; otherwise the original is untouched.

1. In the personal organization on the Fly dashboard, create an organization
   token. Both apps must be in that organization for this one-time migration.
2. In this repository, open Settings → Secrets and variables → Actions. Add a
   repository secret named `FLY_API_TOKEN`. Paste the token only into that secret.
3. Open Actions → Deploy Entry → Run workflow. Enter the Google email allowed
   to sign in and the public Google Web client ID, then run it on `main`.
4. A successful run prints the live address and exact custom-domain DNS records
   in its job summary. Open Entry and use Google to link the configured account.

No Google client secret is used. No Ring credentials, Fly tokens or bootstrap
keys belong in source control. Deployment is not triggered on pull requests.
The token can be revoked after migration and replaced with a token scoped to
the new app for subsequent deployments.

After requesting the custom-domain certificate, use the exact DNS requirements
returned by Fly. Usually this means a CNAME `entry` to the actual new Fly hostname,
plus Fly's ownership TXT record. Do not invent its value. The custom domain is not
live until DNS points correctly and Fly reports an active certificate.

## Development

Python 3.12 and espeak-ng (only for the retained legacy voice endpoints):

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-lock.txt
python -m unittest discover -s tests -v
```

Set `ADMIN_TOKEN` to a strong bootstrap key, then `python server.py`. Use HTTPS for
browser sign-in: host-only Secure cookies are intentionally not sent over public
plain HTTP. `DATA_DIR` selects persistent data and defaults to `data`.

## Verification

39 backend tests pass: schedule boundaries, weekly overnight rules, London daylight
saving, ambiguous/nonexistent one-off clock times, overlapping windows, pause and
resume, cookie/CSRF restrictions, Google email allowlisting, setup-key retirement,
nonce checks, durable sessions, event deduplication and direct unlock behavior.
Browser visual QA and live Google sign-in remain to be verified after deployment.
The existing polling/unlock path was physically tested successfully before this
interface update.

Legacy voice files are retained for possible future development but are absent
from the main interface and never used by the auto-entry mode.

## References

- https://github.com/python-ring-doorbell/python-ring-doorbell
- https://github.com/cmos486/ring-intercom-video
- https://developers.google.com/identity/gsi/web/guides/verify-google-id-token
- https://developers.google.com/identity/gsi/web/guides/get-google-api-clientid
- https://docs.fly.io/networking/custom-domain

This is an unofficial integration, not affiliated with Ring.
