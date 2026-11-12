# Local API reference

What `ffvpn` depends on. Verified against Arman VPN 1.0.18 on 2026-11-12
(upstream `armanlife/arman-vpn-client`). Nothing here is added or patched upstream.

## Where it connects

| What | Where |
|---|---|
| Data folder | `--data-dir` / `FFVPN_DATA_DIR`, else `$XDG_DATA_HOME/arman-vpn` (usually `~/.local/share/arman-vpn`), else `/opt/arman-vpn/user-data` |
| Token | `local-api.token` in the data folder, sent as `X-Local-Token` |
| Base URL | `dashboard.listen` from `config.json`, default `127.0.0.1:10820` |
| Proxy listener | `proxy.listen` in `GET /api/status`, usually `127.0.0.1:10810` |

The local token replaces the dashboard login and the Host check. Every non-GET needs
`Content-Type: application/json` (the server answers 415 otherwise). Errors are
`{"error": "<message>"}` with a 4xx/5xx status.

## Endpoints used

| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/api/status` | — | state, proxy, selection, accounts, exit, modes |
| GET | `/api/locations` | — | `{"locations":[{code,name,city,server,recommended,ping_ms,ping_error}], "pinged_at"}` |
| POST | `/api/location` | `{"country": "FI" \| "fastest" \| ""}` | `{"saved": true}` |
| POST | `/api/system-proxy` | `{"mode": "clear" \| "set" \| "tun"}` | `{"mode": ...}` |
| POST | `/api/power` | `{"on": true \| false}` | `{"on": bool}` |
| GET | `/api/accounts` | — | `{"accounts":[AccountView], "switch_threshold_percent"}` |
| POST | `/api/accounts/{id}/activate` | — | `{"ok": true}` |
| DELETE | `/api/accounts/{id}` | — | `{"ok": true}` |
| POST | `/api/signin` | `{"email", "password"}` | 202 `SignInView` |
| GET | `/api/signin/{id}` | — | `SignInView` |
| POST | `/api/signin/{id}` | `{"value": "..."}` | `SignInView` |
| DELETE | `/api/signin/{id}` | — | `{"cancelled": true}` |

## Status shape (fields `ffvpn` reads)

```json
{
  "state": {"phase": "starting|running|waiting|error|off", "message": "...", "since": "..."},
  "proxy": {"listen": "127.0.0.1:10810", "country_code": "FI", "country_name": "Finland"},
  "selection": "FI|fastest|",
  "selection_name": "Finland",
  "active_account": {"id": "...", "email": "...", "usage": {...}, "used_percent": 7.85},
  "exit": {"ipv4": "...", "city": "Helsinki", "country_name": "Finland"},
  "system_proxy": "clear|set|tun",
  "tun_supported": true
}
```

`phase` changes asynchronously after a location, mode or power change: a restart goes
`running` -> `starting` -> `running`. `POST /api/power` returns before the engine moves,
so poll `phase` (and `proxy.country_code` after choosing a country) instead of trusting
the first status read.

## Locations

- `"fastest"` — the app picks the current lowest-latency server. Not in `/api/locations`.
- `""` — the recommended location. `/api/locations` shows it as the `REC` entry.
- a two-letter code — a specific country from the list.
- `POST /api/location` also clears an exact server, so it always means "this country".

## System proxy modes

| Mode | Meaning |
|---|---|
| `clear` | No system-wide proxy. Apps use `proxy.listen` explicitly (`ffvpn --proxy`). |
| `set` | The GNOME system proxy (`gsettings org.gnome.system.proxy`) points at `proxy.listen`. |
| `tun` | Traffic is routed through a TUN device; needs `tun_supported` (the desktop app provides it). |

## Accounts and sign-in

`AccountView`: `id, name, email, position, enabled, active, status, reason,
reason_code, usage{unlimited,limit_bytes,remaining_bytes,reset_at,checked_at}, used_percent,
has_password, device_registered`. `usage.unlimited` is true when the plan has no quota,
in which case the byte fields are absent. `activate` only works on enabled accounts. `DELETE`
removes the stored account and its tokens — there is no session-only logout.

`SignInView`: `id, email, step, message, error, error_code, captcha, account_id,
password_accepted, updated`. Steps:

`working` -> `need_email_code` | `need_totp` | `need_unblock_code` | `need_captcha` |
`need_email_link` -> ... -> `done` | `failed` | `cancelled`.

A rejected answer comes back as `working` and then the same `need_*` step with `error`
set; for `need_captcha` the step, message and error can stay identical while `updated`
and `captcha` change, so re-prompt on those too. The whole sign-in times out after 15
minutes; `DELETE` cancels it early.
