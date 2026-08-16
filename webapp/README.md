# wkjiten, hosted

The same analysis as the command-line tool, with accounts. Everyone brings their
own WaniKani token and (optionally) their own Jiten, jimaku.cc and
NihongoTracker keys, and sees their own numbers. The local tool in the parent directory is untouched and keeps working
exactly as before — this imports it rather than forking it, so both stay in step.

Deck word lists are cached **once for the whole instance**, because a word list
belongs to a title rather than an account. The second person to look at a series
gets an instant answer, and Jiten sees one download instead of one per user.

There is also a **Together** page: what everyone is watching, reading and has
finished, with the titles more than one of you has picked out.

Sharing is one setting with four levels, each containing the one before it, and
it starts at the first — a reading list is data about a person, not about an
anime, so nothing leaves an account until its owner says so:

| level | who can see it |
|---|---|
| **Just me** | nobody, and nothing is stored |
| **People with an account here** | the others on this instance, on the Together page |
| **Anyone with the secret link** | whoever you send `/s/<token>` to, no account needed |
| **Anyone at all** | the above, plus a permanent `/u/<username>` |

The secret link can be revoked by generating a new one, which is the only way to
take a shared link back. Dropping to **Just me** deletes the stored snapshot
rather than hiding it.

Shared either way: the title, whether it is being watched, planned or finished,
and that person's coverage on it. Never their keys, their reviews, or anything
from WaniKani.

---

## Before you host this

**The Jiten key is not a read-only key.** Jiten's own documentation says it
"carries every permission your account has, including the calls that rewrite
your known words and delete cards. Treat it like your password." If you ask a
friend to paste theirs into your server, you are asking for something
password-shaped, and you become responsible for keeping it.

The NihongoTracker key is the same shape — it is accepted on every endpoint
that account can reach, including the ones that delete logs — and this only
ever reads with it.

So: keys are encrypted with Fernet before they are written, the key lives
outside the database, and the Jiten key is optional — without it a user still
gets the whole kanji analysis, losing only their account's word coverage, the
known-words upload and the list buttons. There is a **Forget my Jiten key**
button in settings. The WaniKani token should be created **read-only**; nothing
here needs write access to WaniKani.

None of that makes a compromised server harmless. Keep the instance small,
private and patched.

**What Jiten allows.** Their guide welcomes "personal projects, hobby tools and
small research scripts". Two things are out: reselling or paywalling access to
Jiten or a relay of its API, and bulk downloading or mirroring significant parts
of the database. A handful of friends is squarely fine; if it grows beyond that,
their guide asks you to check on Discord first. Rate limits are per key, so
users do not share a budget.

---

## Trying it on your own machine first

You do not need a server, a container, or Linux to see whether you like it.
Every launcher says on the tin which system it is for — double-click the one
that matches:

| | on this machine only | shared on your home network |
|---|---|---|
| **Windows** | `Run locally (Windows).bat` | `Run on my network (Windows).bat` |
| **macOS** | `Run locally (macOS).command` | `Run on my network (macOS).command` |

On Linux, run the macOS file from a shell — it is an ordinary bash script.

**On a Mac, clone rather than download the zip.** A zip from the browser arrives
quarantined and macOS refuses to open the launchers; `git clone` does not. If
you already have a downloaded copy, clear it once with
`xattr -dr com.apple.quarantine .` in the project folder.

The first run builds its own virtual environment, installs Flask and
cryptography into it, and starts on <http://127.0.0.1:8770>. It takes a minute;
after that it starts immediately. Everything it writes goes in `webapp/data/`,
separate from the command-line tool's `cache/` and ignored by git — delete that
folder and you are back to nothing.

Watch the window for the invitation link on first run, open it, and create the
first account. That one becomes the admin.

This is Flask's development server on loopback: fine for trying it out and for
your own machine, not what you want other people connecting to. For that, the
container below.

---

## Running it on Bazzite

Bazzite is an immutable image, so install nothing on the host — run it in a
container. Podman is already there.

```bash
git clone https://github.com/bumsar1/Wanikani-jiten.git
cd Wanikani-jiten
printf 'WKJITEN_SESSION_SECRET=%s\n' "$(python3 -c 'import secrets;print(secrets.token_hex(32))')" > webapp/.env
podman build -t wkjiten -f webapp/Containerfile .
```

Then run it, keeping the data in a named volume:

```bash
podman run -d --name wkjiten --restart=unless-stopped -p 127.0.0.1:8080:8080 -v wkjiten-data:/data --env-file webapp/.env wkjiten
```

To have it come back after a reboot, let systemd own it as a user service:

```bash
podman generate systemd --new --name wkjiten --files --restart-policy=always
```

Move the generated `container-wkjiten.service` into `~/.config/systemd/user/`,
then `systemctl --user daemon-reload && systemctl --user enable --now container-wkjiten`.
Run `loginctl enable-linger $USER` so it starts without you logging in.

Watch the first-run output for the invitation link:

```bash
podman logs wkjiten
```

It prints a `/register/<code>` path. Open it, create the first account — that one
becomes the admin — and paste your keys in settings. The admin can mint further
invitations under **invites**; there is no open registration.

---

## Letting someone else in

Both the launcher and the container bind to loopback on purpose, so nothing off
the machine can reach them — your public IP will not help on its own.

**Do not simply bind it wide and forward a port.** The login and password would
travel in clear text, the session cookie with them, and the development server
would be facing the whole internet. Home addresses also tend to change, and
plenty of consumer connections sit behind CGNAT where forwarding is not possible
at all.

### The easy way: Tailscale

For a couple of people this is both the simplest and the safest. No port
forwarding, nothing published, and no domain name to buy:

```bash
tailscale serve --bg 8080
```

That gives you `https://<machine>.<tailnet>.ts.net` with a real certificate,
reachable only by members of your tailnet. The app keeps listening on loopback;
Tailscale sits in front. Your friend installs Tailscale, you invite him, he opens
the link. Set `WKJITEN_HTTPS=1` once it is served over TLS so the session cookie
gets the Secure flag.

On Bazzite the system is immutable, so layer it and reboot:
`rpm-ostree install tailscale`.

### If it really has to be public

Then you want: a domain name, dynamic DNS if your address moves, a forwarded
port, and Caddy or nginx terminating TLS in front of the container — not the
development server. Set `WKJITEN_HTTPS=1`. Registration is invitation-only, which
limits what an anonymous visitor can do, but a login form on the open internet is
still a login form on the open internet.

### Cloudflare Tunnel: a real address, no ports opened

If you want it to be a site with a name rather than something only your tailnet
can see, this is the least painful route. No port forwarding, no fixed address
needed, works behind CGNAT, and the certificate is handled for you. Budget half
an hour; the fiddliest part is choosing a domain name.

**1. A domain on Cloudflare.** Buy one anywhere (Cloudflare Registrar sells them
at cost, roughly $10 a year) and point its nameservers at Cloudflare. Adding the
domain in the dashboard walks you through it.

**2. Make the tunnel.** In the Cloudflare dashboard: *Zero Trust → Networks →
Tunnels → Create a tunnel*, pick **Cloudflared**, name it, and copy the token it
shows you. Then add a **public hostname** on that tunnel:

| field | value |
|---|---|
| subdomain | e.g. `wk` |
| domain | your domain |
| service type | `HTTP` |
| URL | `wkjiten:8080` |

**3. Run it.** Put the token in `webapp/.env`:

```bash
printf 'TUNNEL_TOKEN=%s\n' 'paste-the-token-here' >> webapp/.env
```

Uncomment the `cloudflared` service in `compose.yaml`, set `WKJITEN_HTTPS=1` in
the same `.env`, and bring both up:

```bash
podman-compose up -d
```

`https://wk.yourdomain.com` is now live. The app is still only listening inside
the container network — Cloudflare reaches it through the tunnel, and nothing on
your router changed.

**Worth doing while you are in there.** Under *Zero Trust → Access* you can put a
policy in front of the hostname so visitors have to prove an email address before
they even see the login page. For a two-person tool that turns a public URL back
into a private one, and costs nothing.

Registration here is invitation-only regardless, but a login form on the open
internet is still a login form on the open internet — this is worth ten minutes.

**The trade-off:** your traffic passes through Cloudflare, who can see it. For a
Japanese study dashboard that is a fair trade; decide for yourself.

---

## Environment

| variable | meaning |
|---|---|
| `WKJITEN_SESSION_SECRET` | Signs session cookies. Set it, or everyone is logged out on restart. |
| `WKJITEN_SECRET` | Fernet key for encrypting stored API keys. Generated into `/data/secret.key` if unset. **Back it up.** |
| `WKJITEN_HTTPS` | `1` when served over TLS, so cookies get the Secure flag. |
| `WKJITEN_STALE_HOURS` | How old a WaniKani snapshot may get before a background refresh (default 18). |
| `WKJITEN_DB` | Database path (default `/data/wkjiten.sqlite3`). |

## Running it without a container

```bash
cd webapp
python -m venv .venv && ./.venv/bin/pip install -r requirements.txt
WKJITEN_DB=./data/wkjiten.sqlite3 WKJITEN_SECRET_FILE=./data/secret.key ./.venv/bin/python app.py
```

That is the development server on `127.0.0.1:8080`. Use the container, or
gunicorn, for anything anyone else touches.

## Backups

Everything lives in `/data`: the SQLite database and `secret.key`. Copy both, or
neither — the database alone cannot decrypt the stored API keys.
