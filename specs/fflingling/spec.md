# fflingling — opencode through the Arman VPN

## Goal

Give `opencode` the `lingling` treatment, but riding the Arman VPN instead of Tor:
run opencode behind a local MITM proxy that watches for opencode.ai's `429`, switches
the VPN to a fresh country, and replays the call — so a per-IP free-tier wall never
reaches the user.

Tor gives lingling many simultaneous exits. ffvpn drives the desktop app, which holds
**one** connection in **one** country at a time. So: one upstream, no 5-way race,
rotation on `429` instead. Everything the user sees stays the same as lingling — the
call is transparently replayed on a new address.

## Deliverable

Two files, no third-party Python packages, no packaging:

- `fflingling` — executable, single file, Python 3.10+, stdlib only (`openssl` binary
  is allowed and required).
- `tests/test_fflingling.py` — unittest, loaded with `SourceFileLoader` like
  `tests/test_ffvpn.py`.

Do not modify `ffvpn`. Reuse it by running the `ffvpn` binary (on `PATH`, else the
sibling file next to `fflingling`).

## Behaviour

`fflingling [opencode args...]`

1. Resolve playground: `$FFLINGLING_DATA_DIR`, else `$XDG_DATA_HOME/fflingling`
   (default `~/.local/share/fflingling`). Store `fflingling.log` there.
2. Generate (once, reused after) a local CA and a leaf cert via `openssl` in that
   folder. Leaf SANs: `DNS:opencode.ai`, `DNS:*.opencode.ai`.
3. Connect: `ffvpn connect --proxy <CODE>` with `<CODE>` from `ffvpn ls --codes`
   (first code, or `FFLINGLING_COUNTRY` if set). Failure is fatal, one line.
4. Listen on `127.0.0.1:0` and start opencode with:
   `HTTPS_PROXY`/`https_proxy`/`HTTP_PROXY`/`http_proxy` = `http://127.0.0.1:<port>`,
   `NO_PROXY`/`no_proxy` = `localhost,127.0.0.1`, `NODE_EXTRA_CA_CERTS` = the CA PEM.
   `--no-mitm` omits the CA (blind tunnel only).
5. Return opencode's exit code; no traceback ever escapes to the user.

## Proxy

- `CONNECT host:port`: host matching `opencode.ai` or `*.opencode.ai` is MITM'd;
  every other host is a blind byte tunnel through the VPN's local proxy.
- Hop to the origin **through** the VPN: read `proxy.listen` from `ffvpn`'s data
  (reuse the same discovery as `ffvpn`: `--data-dir`/`FFVPN_DATA_DIR` → XDG →
  `/opt/arman-vpn/user-data`, token not needed for the proxy), `CONNECT` to it,
  then TLS to the origin with the server name.
- One request per connection: client sends one HTTP/1.1 request, we replay it
  upstream with `Connection: close`, stream the response back with `Connection:
  close`, and close. No HTTP/2 ALPN.
- Request body is buffered (Content-Length or chunked) so it can be replayed
  verbatim. Response body streams through untouched.
- Retried statuses: `429`, `502`, `503`, `504`. Everything else — including `403`
  — is passed through unmodified. Max `FFLINGLING_RETRIES` (default 3) replays.
- Retry: pick the next code in the pool (skipping the current one, wrapping), run
  `ffvpn connect --proxy <next>`, then replay. If the connect fails, try the code
  after it; if the pool is exhausted, hand the last upstream response to the client.
- Rotation waits for in-flight opencode.ai requests to finish (up to
  `FFLINGLING_ROTATE_WAIT_S`, default 120, then it goes ahead and says so), and new
  requests wait for the rotation. Concurrent 429s cause **one** rotation, not one
  each (generation counter).
- Timeouts: 30s connect, 300s per upstream read, 300s client read.

## Proof

Append one line per call to `fflingling.log` and print the path at start:

```
16:05:12 #3 {FI} 200 opencode.ai /chat/completions 29.7 KB 7.7s
16:05:12 #3 {FI} 429 -> rotate FI -> SE
```

No second console: opencode owns the terminal.

## Acceptance criteria

- [ ] AC1 `./fflingling` starts ffvpn in proxy mode, points opencode at the local
      proxy, and exits with opencode's status.
- [ ] AC2 `CONNECT opencode.ai:443` and `CONNECT api.opencode.ai:443` are MITM'd;
      `CONNECT opencode.ai.evil.test:443` and `CONNECT example.com:443` are blind
      tunnels.
- [ ] AC3 CA/leaf are generated with `openssl` once and reused; leaf SANs verified
      by `openssl x509 -text`.
- [ ] AC4 A `429` on a buffered POST triggers a country rotation and one replay of
      the identical body/headers; a `200` on the retry reaches the client.
- [ ] AC5 `403`, `200`, `404`, `500` are never retried.
- [ ] AC6 Two simultaneous `429`s produce exactly one rotation (generation counter).
- [ ] AC7 A rotation waits for an in-flight stream and gives up waiting at
      `FFLINGLING_ROTATE_WAIT_S`.
- [ ] AC8 Chunked request bodies are buffered and replayed byte-identically.
- [ ] AC9 Missing `ffvpn`, missing `openssl`, VPN down, or empty location list →
      one-line error, exit 1, no traceback.
- [ ] AC10 `--help` documents usage, `--no-mitm` skips the CA and MITM.

## Out of scope

Parallel lanes / racing, Tor, per-country pools file, self-update, a proof window,
proactive rotation, restoring the user's original country on exit.
