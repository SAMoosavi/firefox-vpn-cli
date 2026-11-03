# ffvpn

A small command-line client for the Firefox VPN desktop app. It talks to the app's
local HTTP API (on `127.0.0.1:10820` by default) using the `local-api.token` from the
app's data folder. Nothing is added to the upstream project: the app must already be
running.

Python 3.10+ standard library only. One file, no install step, no build.

## Install

```sh
chmod +x ffvpn
cp ffvpn ~/.local/bin/ffvpn    # or: ln -s "$PWD/ffvpn" ~/.local/bin/ffvpn
```

Make sure `~/.local/bin` is in `PATH`.

The data folder is auto-detected: `--data-dir` / `FFVPN_DATA_DIR`, then
`$XDG_DATA_HOME/firefox-vpn-client` (usually `~/.local/share/firefox-vpn-client`),
then `/opt/firefox-vpn/user-data`.

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

## Docs

- `docs/api.md` — the local API contract this CLI depends on.
- `reviews/ffvpn-review.md` — the review that shaped the current behaviour. It
  predates the fixes, so its line numbers are from that revision.

## Notes

- `account logout` removes the stored account and its tokens; upstream has no
  session-only logout.
- `connect` with no location resets the selection to `fastest`.
- The Firefox VPN app must be running; ffvpn does not start, restart, or stop it.
