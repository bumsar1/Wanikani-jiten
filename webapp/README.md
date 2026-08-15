# wkjiten, hosted

The same analysis as the command-line tool, with accounts. Everyone brings their
own WaniKani token and (optionally) their own Jiten key, and sees their own
numbers. The local tool in the parent directory is untouched and keeps working
exactly as before — this imports it rather than forking it, so both stay in step.

Deck word lists are cached **once for the whole instance**, because a word list
belongs to a title rather than an account. The second person to look at a series
gets an instant answer, and Jiten sees one download instead of one per user.

---

## Before you host this

**The Jiten key is not a read-only key.** Jiten's own documentation says it
"carries every permission your account has, including the calls that rewrite
your known words and delete cards. Treat it like your password." If you ask a
friend to paste theirs into your server, you are asking for something
password-shaped, and you become responsible for keeping it.

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

## Trying it on Windows first

You do not need a server, a container, or Linux to see whether you like it:

* **Windows** — double-click `Run locally.bat`
* **macOS / Linux** — `./run-locally.sh`

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

## Reaching it from outside

The container binds to loopback on purpose. To let a friend in, put a reverse
proxy with a real certificate in front of it (Caddy is two lines), and set
`WKJITEN_HTTPS=1` so the session cookie is marked secure. Do not simply publish
port 8080 to the internet: the login would travel in the clear.

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
