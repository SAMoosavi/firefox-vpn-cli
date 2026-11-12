GATE: PASS (re-review; blockers: 0, majors: 0). The original gate was FAIL with 3
majors; all three and all seven follow-up items are fixed and verified — see
"Resolution" at the end. The findings below are kept as the original record.

Scope: `fflingling` (whole file), `tests/test_fflingling.py` (whole file), read against
`specs/fflingling/spec.md`, `ffvpn`, and `/tmp/opencode/lingling` for reference.
`ffvpn` is unchanged (`git status` shows only `fflingling`, `specs/`, `tests/test_fflingling.py`
as new). `python3 -m unittest tests.test_fflingling` → 57 tests, OK.

Evidence beyond the diff: four probes against the modules under review, no repo writes.
1. A fake upstream sending `103 Early Hints` then `200` — client received
   `103 ... Connection: close\r\n\r\n200 OK ... hi`, and a 429 behind the 1xx would not
   rotate (`_attempt` returns the 103 status).
2. One `ConnectionAbortedError` injected into `socket.accept` — after it the accept
   thread was gone, the listener stayed open (TCP handshake still succeeds), and a
   `CONNECT` got no reply ever.
3. `proxy_address()` on `[::1]:10810` → `('[::1]', 10810)`, which `socket.create_connection`
   cannot resolve.
4. `FFLINGLING_COUNTRY=fi` with pool `FI SE` → first "rotation" calls `connect FI`
   (same exit) and logs `429 -> rotate fi -> FI`.

## Findings

### blocker

None.

### major

- **[major] fflingling:686-689, 697-711 — a 1xx interim response is treated as the final
  response, so a 429 behind it never rotates and a 103 is delivered as a truncated reply.**
  `_attempt` reads one head and returns its status; `_deliver` then applies `force_close`
  (injecting `Connection: close` into the interim head) and streams whatever follows as the
  "body". Demonstrated above: a `103` before a `200` reaches the client as a 103 head with a
  bogus `Connection: close`, then the raw 200 head and body. A `100 Continue`/`103` before a
  `429` is worse: status 103/100 is not in `RETRYABLE`, so AC4 silently does not fire, and the
  log records `103 ... 0.0 B`. Python's `http.client` (and any client that skips only 100 and
  not 103) would return the 103 as the final response and read the real response as its body.
  → In `_attempt`, loop while `100 <= status < 200 and status != 101`, reading and discarding
  each interim head (optionally forwarding it unmodified to the client) before returning the
  final one; never run `force_close` on an interim head. Treat 101 as unsupported (502/tunnel).
  Add a test with `103` → `429` → `200` asserting one rotation and a clean client stream.

- **[major] fflingling:462-475 — one `accept()` error kills the proxy permanently and silently.**
  `except OSError: return` treats every listener error as "stopped". `ConnectionAbortedError`
  (ECONNABORTED, a client that resets between SYN-ACK and accept — exactly what an aborted
  opencode request can do) and `EMFILE` both land there. The listener socket is left open, so
  the kernel still completes TCP handshakes into the backlog: opencode then gets connections
  that are never read, and every opencode.ai call fails with no entry in the log (the general
  `except Exception` at 474 is unreachable for OSError). Demonstrated with one injected
  ECONNABORTED: accept thread gone, `CONNECT` never answered, `relay._sock` still non-`None`.
  → On OSError, `continue` unless `self._sock is None` (i.e. `stop()` was called); log the error
  and keep accepting. Only return when the listener was deliberately closed. Add a test that
  injects one `ConnectionAbortedError` and then serves a real CONNECT.

- **[major] tests/test_fflingling.py:1117-1162 (whole suite) — origin TLS verification is never
  exercised, so a security regression would pass all 57 tests.** Every test injects the
  `connect` callable (`Scripted`, `make_relay`), and `MitmHandshakeTest` fakes the upstream
  too. `_vpn_connect` (`fflingling:789-811`) is where `ssl.create_default_context()` and
  `server_hostname=host` decide whether the relay will talk to an impostor. Changing it to
  `CERT_NONE`, dropping `check_hostname`, or omitting SNI leaves every test green.
  → Add (a) a test with a local self-signed TLS server asserting `_vpn_connect` raises, and
  (b) one with that cert trusted asserting success; both driven through a fake VPN proxy that
  asserts the `CONNECT host:port` bytes and a 200. This also covers the framing of the VPN
  proxy's own CONNECT response, which is untested today.

### minor

- **[minor] fflingling:227-245, 289-301 — request framing trusts the last duplicate header and
  forwards conflicting framing upstream.** `headers_of` is last-duplicate-wins, `read_body`
  frames on `transfer-encoding` first, then `content-length`; the raw head is forwarded
  verbatim. A client sending both `Content-Length: 5` and `Transfer-Encoding: chunked` gets
  both headers forwarded while we buffer the chunked body (confirmed: forwarded head kept
  `Content-Length: 5` + `Transfer-Encoding: chunked` with a `0\r\n\r\n` body). Duplicate
  differing `Content-Length`s have the same desync. `force_close` also forwards hop-by-hop
  headers (`Keep-Alive`, `TE`, `Trailer`, `Upgrade`, and the tokens named in `Connection`).
  Only the loopback client can reach this, so it is not remotely exploitable, but a framing
  disagreement with the origin is a real bug class. → Reject `TE`+`CL` together and duplicate
  `Content-Length`s with 400; strip hop-by-hop headers (and Connection-listed tokens) in
  `force_close`.

- **[minor] fflingling:499, 789-797 — IPv6 tunnel targets are sent to the VPN proxy without
  brackets.** `host.strip("[]")` keeps a bare `2001:db8::1`, then `_vpn_connect` builds
  `CONNECT 2001:db8::1:443`, which is not a valid authority. `tests/test_fflingling.py:1074-1087`
  asserts exactly that malformed call, so the test locks the bug in. → Keep a bracketed
  authority for the CONNECT line and strip only for `server_hostname`; update the test to
  assert `CONNECT [2001:db8::1]:443`.

- **[minor] fflingling:62-67 — `proxy_address` does not handle a bracketed IPv6 `proxy.listen`,
  unlike ffvpn's `parse_listen`.** `[::1]:10810` → `('[::1]', 10810)`; `socket.create_connection`
  then fails getaddrinfo and every request becomes a 502. → Strip `[`/`]` around the host (or
  reuse ffvpn's `parse_listen` semantics) and add the case to `FolderTest`.

- **[minor] fflingling:193-195 vs fflingling:155 — the MITM predicate accepts any subdomain
  depth, but the leaf SAN is `DNS:*.opencode.ai` (one label).** `is_mitm_host("a.b.opencode.ai")`
  is true (and tested at `tests/test_fflingling.py:155`), so the relay would terminate TLS with
  a certificate whose hostname does not match, and the client gets a hard TLS error instead of
  a blind tunnel. → Limit the predicate to `opencode.ai` or exactly one label below it (or
  issue a SAN that covers the depth actually used). Decide from what opencode really calls;
  currently the two halves disagree.

- **[minor] fflingling:262-325 — request bodies and chunk/line reads are unbounded.** `read_body`
  buffers the whole body (`recv_exact`) with no cap, `read_chunked` trusts the declared chunk
  size, and `recv_line` has no length limit. The relay is the supervisor process: a large or
  malformed body (or any local process that finds the loopback port) can exhaust memory.
  → Cap buffered request bodies (e.g. 64 MiB) and answer 413/400 beyond it; cap a chunk and a
  line length; at minimum use `shortcut:` comments naming the limit.

- **[minor] fflingling:729-733, 770-785 — a rotation has no total deadline and `_enter` waits
  with no timeout.** The pool loop tries every code, each `_run_ffvpn` allowed 300 s
  (`fflingling:81`); with a hung VPN app and a large pool every opencode.ai request sits in
  `_enter()`'s unbounded `cond.wait()` while the socket's 300 s client timeout does not apply.
  The spec's "try the code after it" does not require an unbounded window. → Give the rotation
  an overall deadline (reuse `FFLINGLING_ROTATE_WAIT_S` or add one) after which the pool counts
  as exhausted, and bound `_enter` (answer 503 when the rotation outlives the client timeout).

- **[minor] fflingling:128-177 — certificate reuse trusts four filenames and nothing else.**
  Existence alone is checked, `leaf.csr`/`leaf.ext` are fixed names in a shared directory, and
  there is no lock or atomic rename. A partial generation, two concurrent runs, or a leaf whose
  key no longer matches is reused forever: every MITM handshake fails ("key values mismatch")
  and there is no recovery path. The 825-day leaf is also reused after expiry. → Generate under
  a lock (or per-run temp names) into a temp dir and rename atomically; on reuse verify
  leaf↔key and expiry (`openssl x509 -checkend`) and regenerate if not.

- **[minor] fflingling:897-905 — the VPN is switched before the certificates are prepared,
  reversing the spec's step order (certs are step 2, connect step 3).** On the missing-openssl
  path (AC9) the user's VPN has already been moved to `pool[0]` before the one-line error, and
  the spec explicitly puts that work after the CA. → Run `ensure_certs`/`server_context` (and
  `opencode_binary`) before `connect_country`.

- **[minor] fflingling:897, 198-207 — `FFLINGLING_COUNTRY` is used as the Relay's "current"
  country without normalising or checking it against the pool.** `FFLINGLING_COUNTRY=fi` (or a
  country name, which ffvpn accepts) connects fine but is not a pool code, so the first
  rotation starts at `pool[0]` — demonstrated as `rotate fi -> FI` with `connect FI`, i.e. a
  replay on the same exit, one retry burned, and a log line claiming a move that did not
  happen. → Upper-case it and require membership in `pool` (or resolve it through ffvpn) at
  startup, with a one-line error otherwise.

- **[minor] fflingling:107-108, 770-780 — a rotation counts as successful from ffvpn's exit
  code alone.** `ffvpn connect` returns 0 even when the proxy is still using the old country; it
  only prints `ffvpn: warning: the proxy is still using <code>` to stderr, which `_run_ffvpn`
  discards on success. The relay then sets `self.country` to the new code and replays on the
  old address. → Inspect the captured stderr (or compare the `exit:`/`proxy:` lines in stdout
  with the previous attempt) in `connect_country` and raise on that warning so `_rotate` tries
  the next code.

- **[minor] fflingling:853-861 — `opencode_env` overwrites `NODE_EXTRA_CA_CERTS`.** Any CA the
  user already trusted there (corporate proxy, internal endpoint) is dropped for the whole
  opencode run, including hosts that are only blind-tunnelled and therefore still need it.
  → Read the existing file (if any), concatenate it with `ca.pem` into a generated bundle in
  the data dir, and point the variable at that.

- **[minor] fflingling:529-535 — the MITM TLS socket is closed without `unwrap()`, so no
  close_notify is sent.** CPython's `SSLSocket.close()` only drops `_sslobj` (`_real_close`),
  which the test's own comment at `tests/test_fflingling.py:1108-1110` concedes. Python clients
  happen to see EOF; stricter clients report a truncated TLS stream even though the body is
  complete. → `tls.unwrap()` inside a short-timeout try/except before `close()`.

- **[minor] fflingling:864-865, 920, 924-925 — the child's lifecycle is only covered for
  Ctrl-C.** `SIGTERM` (or `kill`) terminates fflingling without running `finally: relay.stop()`
  and without touching opencode, which is left running against a dead proxy; and a
  signal-killed opencode yields a negative `returncode`, which `sys.exit` truncates to
  247/254 instead of 128+N. → Install a SIGTERM/SIGHUP handler that terminates the child and
  returns 128+signal, and map a negative returncode accordingly (or run opencode in a new
  process group and forward signals).

- **[minor] fflingling:664-669 — the completion log line prints `self.country`, the current
  global, not the country that served the response.** A request completing while another
  thread's rotation has already advanced `self.country` logs the wrong country (reproduced:
  a response served by FI logged as `{SE}`), which defeats the log's stated purpose as proof.
  → Log the country of the final attempt (`old`/`new`), not the shared field.

- **[minor] fflingling:887-890, 139 — the data directory is created 0755, so `ensure_certs`'s
  `mode=0o700` is dead in the CLI path.** `main` creates it first with the default umask and
  `exist_ok=True`; the CA/leaf keys are 0600 (openssl writes them that way), but the log (with
  request paths) and the public certs are world-readable. → `os.makedirs(directory, mode=0o700)`
  plus an explicit `os.chmod` when it already exists.

- **[minor] tests/test_fflingling.py:1004-1017 — the truncated-body test would pass if the
  proxy forwarded a short body.** It asserts only `client.sent.startswith(b"HTTP/1.1 ")`, so a
  200 with a body under the original `Content-Length`, a 500, or a 400 all pass. The stated
  invariant ("never forward a short body under the original Content-Length") is not proven.
  → Assert `startswith(b"HTTP/1.1 400")` and that the injected `connect` was never called.

### nit

- **[nit] fflingling:189-190, 205-207 — `retryable()` and `next_country()` are only called by
  tests; production code uses `status in RETRYABLE` and `rotation_order()`. Delete them or use
  them in `_serve_request`/`_rotate` so the tests cover the real path.

- **[nit] fflingling:257-258 — an over-long head (or a 64 KiB+ status line from the origin) is
  treated as "no head": `_serve_request` returns, `handle` closes, and the client gets a silent
  EOF instead of 431/502. Distinguish "too long" from "closed" and answer accordingly.

- **[nit] fflingling:806-808 — a fresh `ssl.create_default_context()` is built for every MITM
  connection, re-parsing the system CA bundle per request on top of the extra TLS handshake.
  Hoist it to a module-level cached context.

## Not checked / follow-ups

- I did not run against the live app/opencode: no real `ffvpn connect` (would move this
  machine's VPN, currently `system_proxy.mode = tun`, `proxy.country = AR`), no real
  opencode.ai traffic, no real country switch. The 57-test suite was run; no load test.
- Client keep-alive/pipelining is deliberately unsupported (one request per connection,
  `Connection: close` forced both ways), and only that path is exercised — an HTTP/1.1 client
  that ignores the header will see a reset. HTTP/2, WebSocket/Upgrade, redirects and CONNECT
  reuse are out of scope and untested.
- Rotation arithmetic (`_inflight - _blocked`, generation, `_enter`/`_leave`) reads sound and
  I could not construct a deadlock or lost wakeup; the findings above are timeouts, not
  mutual exclusion. The 250-country worst case and behaviour under many concurrent streams
  were reasoned about, not measured.
- CA expiry/reuse, SIGTERM, and the `NODE_EXTRA_CA_CERTS` merge are untested by the suite.
- macOS/Windows behaviour (none of the paths are platform-specialised) was not exercised.

## Resolution (re-review)

Re-review scope: `fflingling` (983 lines), `tests/test_fflingling.py` (1461 lines), read
whole. `python3 -m unittest discover -s tests` → **168 tests, OK** (3.7 s; 64 of them
`test_fflingling`, up from 57, no skips — openssl is installed). `python3 -m unittest
tests.test_fflingling.OriginVerificationTest -v` → both tests run and pass.
Evidence: runtime probes against the real module for every item (no repo writes) plus
mutation testing on a throwaway copy of `fflingling`.

| # | Item | Verdict | Code evidence | Proving test |
|---|------|---------|---------------|--------------|
| 1 | 1xx interim not treated as final | **FIXED** | `fflingling:722-729` — `_attempt` loops while `100 <= interim < 200`, discarding each interim head, then returns the final status/head. Probe: `100`→`429` rotates once and the client gets the `200`; `103` never reaches the client. | `InterimResponseTest` (`tests/test_fflingling.py:1257-1283`). Mutation (loop removed) → 2 failures. |
| 2 | `_accept` survives one `OSError` | **FIXED** | `fflingling:496-501` — on `OSError`, returns only when `self._sock is None` (i.e. `stop()` ran), otherwise sleeps 50 ms and continues accepting. Probe: one `ConnectionAbortedError` → 2nd `accept()` called, handler thread started. | `AcceptLoopTest` (`tests/test_fflingling.py:1286-1307`). Mutation (`except OSError: return`) → failure. |
| 3 | Real origin TLS verification tested | **FIXED** | Production unchanged and correct (`fflingling:849-851`: `create_default_context().wrap_socket(raw, server_hostname=host)`); the test now drives the real `_vpn_connect` through a local CONNECT proxy (`_relay`, `tests/test_fflingling.py:1410-1430`) to a local TLS origin, with only the trust store patched. `api.opencode.ai` accepted; `a.b.opencode.ai` → `SSLCertVerificationError` (one-label SAN mismatch). | `OriginVerificationTest` (`tests/test_fflingling.py:1350-1457`). Mutations: drop `server_hostname` → 2 errors; `check_hostname=False`+`CERT_NONE` → mismatched test fails. |
| 4 | `force_close` drops `Content-Length` with chunked TE | **FIXED** | `fflingling:249, 259-260` — `chunked` computed from `transfer-encoding`; `content-length` line skipped when chunked, regardless of header order. Probe: both orders → `Content-Length` gone, `Transfer-Encoding: chunked` kept; without TE, CL is kept. | **No test.** Indirect `Connection: close` assertions only (`RetryTest`, `ChunkedBodyTest`); a regression to keep both headers would stay green. |
| 5 | `proxy_address` strips `[]` from listen host | **FIXED** | `fflingling:70` — `host.strip("[]")` before the wildcard substitution. Probe: `[::1]:10810` → `('::1', 10810)`, `[::]:10810` → `('127.0.0.1', 10810)`, bare `::1:10810` → `('::1', 10810)`. | **No test** — `FolderTest` only covers `0.0.0.0` (`tests/test_fflingling.py:834-843`). |
| 6 | `is_mitm_host` apex + exactly one label | **FIXED** | `fflingling:205-214` — apex match, else `.opencode.ai` suffix with a non-empty prefix containing no dot. Probe: `opencode.ai`, `API.OPENCODE.AI.`, `a.opencode.ai` true; `a.b.opencode.ai`, `.opencode.ai`, `evilopencode.ai`, `a.opencode.ai.evil` false. | `HostMatchTest.test_opencode_hosts_are_intercepted` / `test_other_hosts_are_not` (`tests/test_fflingling.py:153-162`). Mutation (any depth) → failure. |
| 7 | Over-`MAX_BODY` bodies get a one-line 413 | **FIXED** | `fflingling:21, 323-324, 347-348` cap both CL and chunked buffering; `fflingling:651-655` catches `BodyTooLarge` and sends `413 Payload Too Large` + `Connection: close` + `Content-Length: 0`. Probe: CL 70 MB and oversized chunk both → exactly that 413, and `relay._inflight == 0` afterwards. | `BodyLimitTest` (`tests/test_fflingling.py:1310-1319`). Mutation (`MAX_BODY` lifted) → failure. |
| 8 | Zero-exit "still using" warning fails the rotation | **FIXED** | `fflingling:115-120` — `connect_country` reads stderr (ffvpn prints the warning to stderr and returns 0, `ffvpn:335-338`) and raises; `_rotate` then tries the next code. Probe: warning on exit 0 → raised; with `SE` made to fail, `_rotate` falls through to `DE` (attempts `['SE','DE']`, country `DE`, request replayed to a `200`). | **No test.** An `ffvpn` that exits 0 while warning would silently "succeed" in the suite. |
| 9 | Signal exit code is `128 + N` | **FIXED** | `fflingling:907-909` — `128 - code if code < 0 else code`. Probe: `-9`→137, `-15`→143, `-2`→130, `7`→7. | `ExitCodeTest` (`tests/test_fflingling.py:1322-1331`). Mutation (return raw code) → failure. |
| 10 | Data dir chmod 0700 even when it existed | **FIXED** | `fflingling:933-938` — `makedirs(..., mode=0o700)` plus an unconditional `os.chmod(directory, 0o700)`. Probe: pre-created `0755` dir → `0700`. | **No test.** |

### `entered` / `_leave` balance and the 413 path

Not broken. `_enter()` is paired with `entered = True` (`fflingling:639-640`), and the
single `finally` (`fflingling:706-713`) runs `_leave()` on every exit, including both
413 returns. Runtime probe: after an oversized `Content-Length` 413, an oversized
chunked 413, and a normal `200` request, `relay._inflight` is `0` each time. The 413
response is one status line and has no body; no upstream is opened on that path.

### Remaining blocker / major

None.

### Non-blocking notes (not part of the ten)

- Items **4, 5, 8, 10** have no proving test. The code is correct today (probed), but
  a regression there would pass CI. Cheapest fix: a `force_close` both-headers case, a
  `[::1]` case in `FolderTest`, a zero-exit stderr-warning case in `FfvpnCallTest`,
  and an existing-`0755`-dir case around `main`.
- `101 Switching Protocols` sits inside the skipped `100 <= interim < 200` range
  (`fflingling:727`), so an upgrade is discarded rather than rejected: the relay reads
  the upgraded byte stream as a head (garbage forwarded if it contains `\r\n\r\n`,
  otherwise a hang up to `READ_TIMEOUT` then 502). This is not a regression — the old
  code also broke upgrades, one way or another — and opencode.ai's API does not need
  WebSockets for the rotation AC, so it stays minor.
- The 413 is written while a large upload may still be in flight, so a client that has
  not finished sending can in principle see a reset instead of the 413. Theoretical for
  opencode's JSON bodies, and the cap itself is the point.

### What I did not check

- No live `ffvpn connect` / country switch and no real opencode.ai traffic (unchanged
  from the original review; the VPN is still left alone).
- The mutation runs used a copied script, not the repo (read-only on source).
- Concurrency, SIGTERM and CA reuse from the original "Not checked" list remain
  untested/unexercised.
