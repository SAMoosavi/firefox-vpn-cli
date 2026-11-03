GATE: FAIL (blockers: 1, majors: 2)

Scope: `ffvpn` (whole file), `tests/test_ffvpn.py`, `tests/test_ffvpn_acceptance.py`,
`README.md`. Not a git repo, so the whole files were reviewed. I also checked the
upstream API contract against the running app on this machine (read-only) and against
the upstream Go source for the parts where a live test would have mutated the VPN.

Evidence used beyond the files under review:
- Live `GET /api/status`, `/api/accounts`, `/api/locations` with the machine's token
  (read-only): `state.phase="running"`, `selection="FI"`, `proxy.country_code="FI"`,
  `used_percent=7.96`, `usage.{limit,remaining}_bytes`, `tun_supported=true`,
  `system_proxy="tun"`, 1 account, no `ping_ms` in locations.
- Live wire-format probes, non-destructive: `DELETE /api/signin/does-not-exist`
  with `Content-Type: application/json` and no body → `200 {"cancelled":true}`;
  the same without the header → `415`; `POST /api/accounts/does-not-exist/activate`
  → `400 {"error":"only enabled accounts can be activated"}`.
- Upstream source (read to settle behaviour the live API makes expensive to exercise):
  `core/internal/service/{dashboard_api.go,signin_flow.go,daemon.go,connect.go,engine.go}`,
  `core/internal/service/dashboard_api.go` status view, `core/internal/fxa/challenge.go`.
- `python3 -m unittest discover -s tests -t tests` → 65 tests, OK.

## Findings

### blocker

1. **`ffvpn:45-50` (and `ffvpn:139-145`) — an explicit data folder is silently
   ignored, so commands hit a different app instance, and the error names the wrong
   path.** `find_data_dir` treats `--data-dir` / `FFVPN_DATA_DIR` as just the first
   candidate; when its `local-api.token` is missing it falls through to
   `~/.local/share/firefox-vpn-client`. Demonstrated on this machine:
   `./ffvpn --data-dir /tmp/does-not-exist account ls` listed the *real* account, and
   `FFVPN_DATA_DIR=/tmp/does-not-exist ./ffvpn ls --codes` contacted the real app. With
   no token anywhere, the error names the default folder
   (`/tmp/no-xdg/firefox-vpn-client`), not the folder the user passed.
   Why it matters: `--data-dir` is the only isolation mechanism; a user pointing at a
   scratch folder to *test* `account logout --yes` deletes the real account and its
   tokens irreversibly. The wrong path in the error message also makes diagnosis
   impossible. The test at `tests/test_ffvpn.py:147-154` locks the fallthrough, but
   only covers the no-explicit-dir case; the dangerous case is untested.
   Fix: make an explicit value authoritative — `if explicit or os.environ.get("FFVPN_DATA_DIR"): return that`
   — and let `read_token` produce `no local API token in <that dir>`. Adjust README
   lines 19-21, which currently describe the flag as a mere candidate.

### major

2. **`ffvpn:509-511` (+ `514-528`) — a refused CAPTCHA answer wedges `account login`
   in a silent 15-minute poll.**
   The "have I already asked this" key is `(step, error, message)`. The app re-asks
   `need_captcha` after a wrong answer with the *same* step, the *same* message
   ("Firefox Accounts asks for a CAPTCHA…") and an *empty* `error`; only the `captcha`
   image (and `updated`) changes. Upstream: `signin_flow.go` calls
   `s.ask(ctx, stepCaptcha, <fixed message>, signInProblem{}, image)` on every ask, and
   `fxa/challenge.go` re-issues the captcha after a wrong answer
   (`maxPostBackRounds = 8`, "Fastly issues a new image after a wrong CAPTCHA answer").
   So after a wrong CAPTCHA: `key == prompted`, the branch is skipped, nothing is
   printed, no new image is opened, the loop polls every second until
   `SIGNIN_TIMEOUT` (line 488/542-543) and exits 1 with "sign-in timed out" — the user
   cannot tell what happened and cannot retry. Wrong email/TOTP codes do *not* wedge
   (the server changes `error` text via `codeProblem`), which is why only the CAPTCHA
   path is broken.
   Fix: include the ask identity in the key, e.g.
   `key = (step, error, message, view.get("updated"))` (or `view.get("captcha")`).
   Add the missing test: a fake that returns `need_captcha` with image A, then image B,
   same message/error, and assert `ask` was called twice.

3. **`ffvpn:263-288` and `ffvpn:293-296` — status is read once immediately after an
   asynchronous POST, so `connect`/`disconnect` can report the opposite of what
   happened.**
   `POST /api/power` returns before the engine changes: with the local token it calls
   `Daemon.Connect()`, which only sets the flag and spawns `giveUpUnlessConnected`
   (`connect.go`); `SetOn(false)` only sets `off` and sends a non-blocking restart
   (`daemon.go: SetOn` → `setOn` → `RequestRestart`). The supervisor goroutine (also
   `daemon.go`) is what sets `PhaseStarting`/`PhaseOff` and stops/starts the engine.
   `cmd_disconnect` therefore can print `state.message` = **"Proxy running"** right
   after asking the app to stop; `cmd_connect` can see `phase="starting"` (during the
   restart that `SetLocation` triggers) and exit 1 with "the VPN is starting", or see
   `phase="running"` — which per `engine.go` only means the listener is open ("An engine
   starts asleep… nothing is sent to Mozilla until something needs the upstream") — and
   exit 0 without any upstream check. `POWER_TIMEOUT = 180` (line 20) is dead code
   because the POST never waits.
   Fix: use the existing 180 s budget to poll `/api/status` until the phase reaches the
   requested state (for connect: `running` **and** not `starting`, plus fail on
   `connect_failure` / `proxy.wake_error`), then print; do the same (shorter) for
   disconnect. One small loop reused by both.
   Not reproduced live (would have changed this machine's VPN); verified from the
   server source. Suggested check: loop `ffvpn disconnect; ffvpn connect FI` ten times
   and watch for "Proxy running" after disconnect / exit 1 after connect.

### minor

4. **`ffvpn:58-67` — IPv6 listen addresses produce an unusable URL.**
   `parse_listen("[::1]:10820")` and `parse_listen("::1:10820")` both return
   `"::1:10820"` (verified by calling the function), so the base becomes
   `http://::1:10820` and every command fails with "cannot reach …". `host.strip("[]")`
   removes the brackets and never adds them back. Fix: after stripping, `if ":" in host: host = f"[{host}]"`.

5. **`ffvpn:59`, `ffvpn:77` — a non-string `dashboard.listen` crashes with a raw
   `AttributeError`.**
   `dashboard_address`'s `try` (71-76) does not cover `parse_listen`, and
   `parse_listen` does `(listen or "").strip()`. A config with `"listen": 10820` raises
   `AttributeError: 'int' object has no attribute 'strip'` (verified), which `main`
   prints verbatim (line 687) instead of falling back. Fix: `if not isinstance(listen, str): return DEFAULT_ADDRESS`.

6. **`ffvpn:413-418` — `account logout` with zero accounts says "more than one
   account".**
   Confirmed by calling it with `accounts=[]`:
   `more than one account; name the account to remove (see 'ffvpn account ls')`.
   Fix: `if not accounts: raise FfvpnError("no accounts to remove")` before the
   `one`/`many` branch.

7. **`ffvpn:482-552` — the server-side sign-in is only cancelled on Ctrl-C.**
   The `try` starts at 492, after the initial `POST /api/signin` (482), and only
   `KeyboardInterrupt` cleans up (546-552). An `FfvpnError` from `ask()` (EOF/no input
   on a pipe), from `save_captcha`, or from `POST /api/signin/{id}` propagates without
   `DELETE`, leaving the session (and a pending FxA verification) alive for the app's
   15-minute timeout. Fix: `try/finally` around the loop with a `done` flag; cancel in
   the `finally` unless done, and move the initial POST inside the `try`.

8. **`ffvpn:616-619` — `--data-dir` only parses before the subcommand.**
   `ffvpn ls --data-dir /tmp` exits 2 ("unrecognized arguments"); only
   `ffvpn --data-dir /tmp ls` works (verified). Since finding 1 makes people reach for
   the flag, the natural placement failing is a trap. Fix: put `-d/--data-dir` on a
   `parents=[common]` parser so it works in both positions, or document "before the
   command" in the README usage block.

9. **`ffvpn:309`, `ffvpn:318-320` — `ls` marks nothing in `fastest` mode, which is what
   plain `ffvpn connect` selects.**
   `selection` comes from `proxy.country` and is documented upstream as `""`
   (recommended), `"fastest"`, or a country. With `"fastest"` no code matches and the
   `selection == ""` recommended fallback is not used either, so `ffvpn ls` shows no
   `>` row after the default `connect` — contrary to the README ("with the current one
   marked"). Live data confirms `selection="FI"` marks correctly, so only the fastest
   path is dead. Fix: prefer `status.proxy.country_code` for the marker and fall back
   to `selection` (this also reflects a failover to a stand-in server).

10. **`ffvpn:561`, `ffvpn:567-570` — bash completion breaks on a leading `-d DIR` and
    never completes account names.**
    `sub="${COMP_WORDS[1]}"` is `-d` for `ffvpn -d DIR connect <TAB>`, so the `*)`
    branch offers subcommands. `ffvpn account set <TAB>` / `account logout <TAB>`
    (both take an existing account) hit the `account)` branch and offer
    `ls usage set logout login` again, not accounts. Fix: scan `COMP_WORDS` for the
    first non-option word (skipping the value of `-d`), and add a machine-readable
    `ffvpn account ls --ids` (one line, like `ls --codes`) for both bash and fish.

11. **`ffvpn:146`, `ffvpn:482` — the local token and the account password are sent as
    plaintext HTTP to whatever `dashboard.listen` says, with no warning when that is
    not loopback.**
    `parse_listen` only maps the *wildcard* to 127.0.0.1; a specific LAN address is used
    as-is. The app itself exposes `status.dashboard.plaintext_over_network` for exactly
    this. Narrow (server installs, which require dashboard auth, but the token is still
    accepted there), but a one-line check is cheap. Fix: warn on stderr when the host is
    not loopback, and refuse `account login` in that case without an explicit
    `--allow-plaintext`.

12. **Test coverage misses every bug above and the polling loop entire.**
    In `tests/test_ffvpn_acceptance.py:384-521` every login fake asserts `get` is never
    called, so the poll loop, message de-duplication and the `prompted` key — where
    finding 2 lives — are untested. Also missing: explicit `--data-dir` with no token
    (finding 1), `selection="fastest"` in `ls` (finding 9), zero accounts for logout
    (finding 6), an IPv6 listen (finding 4), a non-string listen (finding 5), a 200 with
    a non-JSON body for `_request`, and an `FfvpnError` (not Ctrl-C) cancelling a
    sign-in (finding 7). The 65 existing tests pass, so this is a gap, not a failure.

### nit

13. **`ffvpn:372-373` — `used_percent` is rounded to a whole number.** Live it prints
    `8%` for 7.96%. That is fine as a display; the only concrete edge is 94.6 % printing
    `95%` next to the app's 95 % switch threshold, and 99.6 % printing `100%` while
    quota remains. `:.1f` or showing the app's `switch_threshold_percent` would remove
    the ambiguity. Not worth blocking.

14. **`ffvpn:466-469` — every CAPTCHA leaves a PNG in `$TMPDIR` forever.** Delete it
    after the answer (or reuse one path per sign-in). Also consider deleting on
    sign-in failure.

15. **`ffvpn:120-126` — `_request`'s non-JSON/timeout messages could be sharper.**
    A 200 with a non-JSON body becomes `unexpected response from <base>` without the
    path or status, and the `is the Firefox VPN app running?` tail is printed for a
    30 s timeout on a live app. Include the method/path and the status when known.

16. **`ffvpn:685-688` — the bare `except Exception` is acceptable for a CLI, but it is
    what turns finding 5 into `'int' object has no attribute 'strip'`.** Keep the
    friendly message and add an escape hatch (`if os.environ.get("FFVPN_DEBUG"): raise`)
    so a real bug is reportable.

17. **`ffvpn:411-412` + `ffvpn:218-238` — `account logout --yes` resolves a fuzzy
    unique substring and then irreversibly deletes credentials.** Fine for `account
    set` (reversible); for logout, requiring an exact id/email when `--yes` is given
    (or printing the resolved address and asking once) is safer. Low risk, but the
    action is irreversible.

18. **`ffvpn:356`, `ffvpn:387` — with zero accounts `account ls` and `account usage`
    print only the header row.** Print `no accounts` (one line) so the output is not
    mistaken for a parse failure.

## Confirm / refute of the six items you flagged

| Item | Verdict |
|---|---|
| (a) `find_data_dir` fallback | **Confirmed, blocker.** `ffvpn:45-50`. Live-demonstrated: `--data-dir` and `FFVPN_DATA_DIR` both silently used the real app; error names the default. |
| (b) IPv6 listen | **Confirmed, minor.** `ffvpn:58-67`; `[::1]:10820` and `::1:10820` → `::1:10820`. |
| (c) `used_percent` `:.0f` | **Confirmed as a fact, nit not a bug.** `ffvpn:373`; only 94.6→`95%` is a concrete annoyance. |
| (d) zero-account logout message | **Confirmed, minor.** `ffvpn:415-418`; reproduced by calling the command with `accounts=[]`. |
| (e) bare `Exception` in `main` | **Acceptable for a CLI; keep it, add `FFVPN_DEBUG`.** `ffvpn:685-688`. Not a finding on its own, but see nit 16 and finding 5. It also catches `BrokenPipeError`; I checked `ffvpn ls | head -2` and the output is small enough that no broken pipe occurs, so no failure there. |
| (f) bash completion | **Confirmed, minor.** `ffvpn:561`, `567-570`. Both parts: the `-d` offset and the missing account names. |

## Refuted / verified fine (so they are not re-litigated)

- **DELETE without a body:** works. Live `DELETE /api/signin/does-not-exist` with
  `Content-Type: application/json` and no body → `200 {"cancelled":true}`; without the
  header → `415 {"error":"requests must be JSON"}`. `ffvpn:94-100` sends the header for
  every non-GET, so both `account logout` and sign-in cancel are fine.
- **`account set` on a disabled account does not need a pre-check.** The handler
  validates enabled before anything else and answers `400 {"error":"only enabled
  accounts can be activated"}` (verified live with a bogus id), and `ffvpn` surfaces
  that text. A local pre-check would only duplicate the message.
- **`_request` handles a JSON decode failure** (`ffvpn:120-121`) with a clean
  `FfvpnError` instead of a traceback; only the wording is weak (nit 15).
- **No proxy leak:** `ProxyHandler({})` at `ffvpn:89` works — `ffvpn ls --codes` with
  `http_proxy`/`HTTP_PROXY` set to a black-hole address still succeeded.
- **Password handling is sound:** never in argv (`ffvpn:664-666` has no `--password`),
  `getpass` on a TTY, read from stdin otherwise, never printed (tests at
  `test_ffvpn.py:540-556` cover this).
- **Test payloads are real, not invented:** live `used_percent` is at the account
  level (7.96), `usage.limit_bytes`/`remaining_bytes`/`reset_at`,
  `selection`/`selection_name`, `proxy.country_code`, `tun_supported`, `state.phase`,
  and `locations[].ping_ms` all match the fakes. The CAPTCHA field is `captcha` and it
  is a `data:image/...` URI, which `save_captcha` handles.
- **The `exit:` line is not stale in the steady state:** `daemon.go` clears `d.exit`
  when a new engine starts; the only way to print the previous engine's exit is the
  same race as finding 3.
- **Sign-in steps other than CAPTCHA re-prompt correctly:** a wrong email/TOTP/unblock
  code changes `error`, so the `(step, error, message)` key changes. `need_email_link`
  posts `{"value": ""}` and the server ignores the value — the test's locked payload is
  correct.

## Not checked / follow-ups

- I did **not** run `connect`, `disconnect`, `account set`, or `account login` against
  the live app (all would change this machine's VPN or need real credentials). Their
  behaviour was checked against the upstream source; finding 3 is source-based and
  should be confirmed by the loop described there.
- No end-to-end Firefox Account sign-in (no credentials), so the CAPTCHA wedging was
  established from `signin_flow.go` + `challenge.go`, not observed live.
- Zero/many-account behaviour was exercised with fakes and direct function calls only;
  the machine has exactly one account.
- The stated acceptance criteria are not in scope here, so I reviewed behaviour against
  the help text and README, not against an AC list.
- I did not audit the app's own security (TUN helper, dashboard auth); only ffvpn's
  handling of the token/password.
- No file in the project was modified; the only server calls were GETs plus the two
  non-destructive probes noted above.
