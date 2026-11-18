# Vector room portal

The portal runs as a separate local process. A phone can switch rooms, edit alarms, dismiss or snooze a ringing alarm, adjust message color and ring brightness, and choose how Vector handles game spoilers. Scheduled times use the resident’s timezone. Saving an existing alarm keeps its name. Creating another alarm with that name or saving a stale edit returns a conflict. Refresh the room before trying again.

## Run

Copy `portal.py`, `portal_ui.html`, `portal_ui.css` and `portal_ui.js` into the same directory. The standalone process needs Linux, Python 3.12–3.14, SQLite from the standard library, and the repository’s pinned `httpx==0.28.1`, `websockets==15.0.1` and `h11==0.16.0`, including their dependencies. The public controller image already includes them. All browser assets are local.

Mount the operator’s config read-only at `/config/portal.json`. This complete loopback example is for development. For phones, use the portal and HA origins reachable from their browser. Each room ID must match its socket filename.

```json
{
  "portal_origin": "http://127.0.0.1:17868",
  "ha_origin": "http://127.0.0.1:8123",
  "rooms": [
    {"id": "den", "label": "Den"},
    {"id": "study", "label": "Study"}
  ],
  "allowed_user_ids": []
}
```

Run from the directory containing the four files, or substitute `python -m vector_puck.portal` for `python portal.py` in an installed package.

```sh
python portal.py --config /config/portal.json --control-dir /control --state-dir /state --bind 127.0.0.1 --port 17868
```

- **Network.** For household phones, terminate HTTPS on a LAN-only reverse proxy and preserve the configured portal Host, including any nondefault port. In a separate container, bind on its private interface or use `--bind 0.0.0.0` behind that restricted proxy. The portal listens for HTTP and defaults to loopback on port 17868. The proxy must enforce local ingress even when it rewrites forwarding headers. Exclude callback query strings, cookies and request/response bodies from proxy logs.
- **Origins.** Use a dedicated portal hostname. Private-IP client IDs are supported. Plain HTTP supports loopback and trusted local networks, but sends bearer cookies and authorization traffic in cleartext. Use HTTPS on both origins for household use. Install the household CA in the portal’s trust store if HA uses an internal CA. HA must resolve to a loopback, RFC 1918 or IPv6 unique-local address inside the container. The portal checks the address used for each HA connection. [Home Assistant client ID validation](https://github.com/home-assistant/core/blob/dev/homeassistant/components/auth/indieauth.py)
- **Room access.** Mount only the allowed room sockets at `/control/`, preferably by mounting their dedicated directory read-only so recreated sockets remain visible. Use the resident’s numeric UID for its kernel peer check. The portal issues HTTP over these Unix sockets. It requires no device addresses, HA service token, resident database mount, or microphone access.
- **State.** Supply a dedicated writable persistent directory owned by the portal UID, preferably mode `0700`. The portal creates `/state/paired-phones.sqlite3` mode `0600`. Only phone grant IDs, labels, issuance/expiry times and SHA-256 digests of random 256-bit device credentials are persisted. Keep this directory across image replacements. SQLite needs write access to the directory as well as the file. HA access/refresh tokens, authorization flows, codes and short sessions live in RAM.

## Access and pairing

HA sign-in uses a browser-bound, single-use state and S256 PKCE. The server exchanges the code and checks the user returned by the configured HA WebSocket `auth/current_user` command at sign-in and on protected requests. Owners and admins can manage configured rooms. To enroll an existing non-admin HA account, add its exact 32-hex user ID to `allowed_user_ids` and restart the portal. That account gains Vector room access only. HA sessions expire at the earlier of access-token expiry or one hour, with a 30-minute idle timeout. [Home Assistant authentication](https://developers.home-assistant.io/docs/auth_api/) · [WebSocket protocol](https://developers.home-assistant.io/docs/api/websocket/) · [Current-user fields](https://github.com/home-assistant/core/blob/dev/homeassistant/components/auth/__init__.py)

1. Sign in as the HA owner. Under **Caregiver phones**, enter a recognizable phone label and select **Get pairing code**.
2. On the other phone, open the same local portal and enter the eight-digit code under **Pair this phone**. A code expires after five minutes, is consumed once, and is replaced when that owner issues another. The portal permits five attempts per connecting IP and forty in total per five minutes.
3. The phone can manage every configured Vector room for **30 days from pairing**, including across portal restarts. Room sessions renew from its device cookie. Pairing creates no HA account. Caregivers and ordinary admins cannot issue codes, list phones or revoke them.
4. The owner can select **Refresh phones**, then **Revoke** beside a phone. The next protected request from that phone, including an already-open session, is denied. Signing out on a paired phone also removes that phone’s grant. There is a limit of twelve paired phones, 64 open sessions and 64 pending HA sign-ins.

Session and device cookies are host-only, HttpOnly and SameSite=Strict. HTTPS adds Secure and the `__Host-` prefix. The temporary OAuth return cookie uses SameSite=Lax. Authenticated writes also require the exact browser Origin and a per-session CSRF header. Pairing codes are manually entered and stay out of links.

## Verify

```sh
PYTHONPATH=src python -m unittest discover -s tests -p 'test_portal.py' -v
```

The suite uses temporary state, synthetic identities, real loopback HTTP/WebSocket exchanges and the resident’s real Unix socket and alarm database. An offline room remains readable when its resident socket is available. Writes require an online state. If a request times out after submission, refresh before retrying. The browser reports success only after the portal reads back the requested change.
