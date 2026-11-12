# ffvpn

A small command-line client for the Arman VPN desktop app. It talks to the app's
local HTTP API (on `127.0.0.1:10820` by default) using the `local-api.token` from the
app's data folder. Nothing is added to the upstream project: the app must already be
running.

Python 3.10+ standard library only. One file, no install step, no build.
`fflingling` in the same folder puts opencode behind the VPN and rotates the
country on a rate limit (see below).

## Install

```sh
chmod +x ffvpn
cp ffvpn ~/.local/bin/ffvpn    # or: ln -s "$PWD/ffvpn" ~/.local/bin/ffvpn
```

Make sure `~/.local/bin` is in `PATH`.

The data folder is auto-detected: `--data-dir` / `FFVPN_DATA_DIR`, then
`$XDG_DATA_HOME/arman-vpn` (usually `~/.local/share/arman-vpn`),
then `/opt/arman-vpn/user-data`.

## Usage

```sh
ffvpn connect                  # connect to the fastest location
ffvpn connect FI               # country code, any case
ffvpn connect finland          # unique match on name or city
ffvpn connect rec              # recommended location
ffvpn connect --proxy FI       # local proxy only, no system-wide proxy
ffvpn connect --system-proxy   # set the GNOME system proxy
ffvpn connect --tun FI         # route traffic through a TUN device
ffvpn disconnect

ffvpn ls                       # locations, with the current one marked
ffvpn ls --codes               # just the codes, space separated

ffvpn account ls
ffvpn account usage
ffvpn account set bob@example.com
ffvpn account logout           # asks first; --yes skips the prompt
ffvpn account login            # interactive Firefox Account sign-in
```

`connect` with no location always selects `fastest`; it does not keep the previously
saved country. With no mode flag the current system-proxy mode is left alone. The
`--tun` flag is rejected if the app reports TUN as unsupported.

## Completions

```sh
# bash
ffvpn completion bash > ~/.local/share/bash-completion/completions/ffvpn

# fish
ffvpn completion fish > ~/.config/fish/completions/ffvpn.fish
```

The completion scripts call `ffvpn ls --codes` for live location names.

## fflingling — opencode through the VPN

`fflingling` is the `lingling` idea with the Arman VPN instead of Tor: run
`opencode` behind a local proxy, and when opencode.ai answers `429` (the free tier
is per-IP) switch the VPN to another country and replay the same call. You never see
the wall.

Tor lets lingling race five exits at once. The VPN app holds **one** connection in
**one** country, so a rotation is a real reconnect (a few seconds) instead of a
parallel lane. Same behaviour, one address at a time.

```sh
./fflingling                 # = opencode, with the VPN and rotation
./fflingling -- --help       # everything after `--` goes to opencode
./fflingling --no-mitm       # tunnel only: no local CA, no 429 handling
```

It needs `ffvpn` (this repo or `PATH`), `openssl`, `opencode` on `PATH`, and the
Arman VPN app signed in.

What it does:

1. `ffvpn connect --proxy <CC>` to the first listed country (`FFLINGLING_COUNTRY`
   overrides), so only the proxy is routed — not the whole system.
2. Generates a local CA and an `opencode.ai` / `*.opencode.ai` certificate once in
   the data folder, and hands the CA to opencode via `NODE_EXTRA_CA_CERTS`.
3. `opencode.ai` (and one label under it) is terminated locally and re-encrypted to
   the real origin through the VPN. Every other host is a blind tunnel.
4. `429`, `502`, `503`, `504` → next country, then the buffered request is replayed
   verbatim. `403` and everything else pass straight through. Rotation waits for
   in-flight calls to finish (up to `FFLINGLING_ROTATE_WAIT_S`).
5. One line per call in `$XDG_DATA_HOME/fflingling/fflingling.log`
   (`FFLINGLING_DATA_DIR` overrides): time, call, country, status, host, path,
   bytes, seconds — plus a `rotate FI -> SE` line when it moves.

Environment: `FFLINGLING_RETRIES` (default 3), `FFLINGLING_ROTATE_WAIT_S` (120),
`FFLINGLING_COUNTRY`, `FFLINGLING_DATA_DIR`.

Notes:

- `connect --proxy` sets the app's system-proxy mode to `clear`, so a TUN session
  is replaced by the local proxy. The last country stays connected on exit.
- An `opencode` background service that was already running before the launch will
  not use the proxy. Close it, then start `fflingling`.
- Rotation is global: one VPN, one move at a time.

## Docs

- `docs/api.md` — the local API contract this CLI depends on.
- `reviews/ffvpn-review.md` — the review that shaped the current behaviour. It
  predates the fixes, so its line numbers are from that revision.
- `specs/fflingling/spec.md` — what `fflingling` must do.
- `reviews/fflingling-review.md` — its review, findings and resolutions.

## Notes

- `account logout` removes the stored account and its tokens; upstream has no
  session-only logout.
- `connect` with no location resets the selection to `fastest`.
- The Arman VPN app must be running; ffvpn does not start, restart, or stop it.
