# google integration: sending login codes through Gmail

The kernel's one dependence on Google is narrow and one-directional: it *sends*. When a symbiot asks for a login code, the hosted path puts that code in their inbox by calling the **Gmail API** as a real Google Workspace mailbox. Nothing is read, no mailbox is watched, no OAuth dance happens at runtime — a single `gmail.send` call, and that is the whole surface. This document is the deep reference for standing that path up, wiring it into a box, and — the reason it exists as its own page — rebuilding it from nothing the day the credential is lost.

The delivery itself lives in [`services/adapters/email_client.py`](../services/adapters/email_client.py) (`GmailEmailClient`); this page is the *operational* half, the moves you make in Google's consoles that the code assumes have already happened.

## what the integration actually is

The Gmail client authenticates as a **GCP service account with domain-wide delegation** — a headless machine identity, no interactive login, ideal for a server that must send mail at 3am with no human present. The subtlety worth holding onto: the service account **holds no mailbox of its own**. It doesn't send *as itself*. Domain-wide delegation lets it **impersonate a real Workspace user** — the address in `GMAIL_SENDER` — and send as them, so the mail arrives from a person the recipient recognises, not from a robot account.

Three properties fall out of that design, and each is deliberate:

- **Send-only, by scope.** The only authority ever granted is `https://www.googleapis.com/auth/gmail.send`. The service account cannot read the impersonated mailbox, cannot list it, cannot delete from it. If the key leaks, the blast radius is "someone can send mail as the sender", not "someone can read the sender's mail".
- **It refuses rather than pretends.** Until both `GMAIL_CREDENTIALS_FILE` and `GMAIL_SENDER` are set, the client raises instead of silently no-op'ing. A box that thinks it can email but can't will fail loudly on the first send, not swallow a login code.
- **The key is per-box and never committed.** Each box carries its own JSON key on disk, matched by `.gitignore` (`*-sa-creds.json`, `gmail-credentials*.json`, `*.gmail.json`). The Google libraries and the key load lazily on the first send, so import and the test suite never touch either — the suite runs entirely on the fake client.

If a box has no Workspace at all — a home server, a fully-local setup — **none of this applies**: leave both env vars blank and the login code is written to `OTP.txt` instead of emailed. See the kernel README's "Running fully local" section. Everything below is only for a box that genuinely sends.

## prerequisites

Before touching a console, three things have to be true:

- **A GCP project** that will own the service account. The Joy's is `the-joy-496315`; the Gmail API must be *enabled* in it (APIs & Services → Library → "Gmail API" → Enable).
- **Google Workspace admin access** on the sender's domain. Domain-wide delegation is authorised in the Workspace admin console, and only an admin can do it — a plain project owner cannot.
- **A real sender mailbox** in that domain — an actual live Workspace user the service account will impersonate. This becomes `GMAIL_SENDER`. It must be a deliverable account, not an alias or a placeholder.

## the setup, end to end

The order matters: GCP first, because the Workspace step needs the numeric client ID that only exists once the service account is made.

### in the GCP console (project `the-joy-496315`)

1. **Enable the Gmail API** — APIs & Services → Library → "Gmail API" → **Enable** (or confirm it already reads *Manage*).
2. **Create the service account** — IAM & Admin → Service Accounts → **Create service account**. Name it `joy-gmail-client` (the account ID auto-fills from the name). Grant it **no project roles** — it authorises via delegation, not IAM, so the "grant access" step is left empty. Click through to Done.
3. **Create a JSON key** — open the account, **Keys → Add key → Create new key → JSON**. The browser downloads the key immediately, and Google keeps no copy — this file is the only one that will ever exist for this key. This becomes `GMAIL_CREDENTIALS_FILE`.
4. **Note the numeric client ID.** It's the account's OAuth2 client ID (`client_id` in the downloaded JSON, or the "Unique ID" on the Details tab) — a long number like `105769668288219405564`. The Workspace step needs it.

### in the Google Workspace admin console (the sender's domain)

5. Security → Access and data control → **API controls** → **Manage Domain-Wide Delegation** → **Add new**.
6. Paste the **client ID** from step 4 and authorise **exactly one** scope:

   ```
   https://www.googleapis.com/auth/gmail.send
   ```

   Confirm the new row appears listing that client ID against that single scope.

### on each box (local and server)

7. Place the JSON key somewhere gitignored — the Joy names it `joy-gmail-client-sa-creds.json` at the kernel repo root, matched by the `*-sa-creds.json` pattern in `.gitignore`. Set `GMAIL_CREDENTIALS_FILE` to its path and `GMAIL_SENDER` to the Workspace mailbox to impersonate.

## if the service account is ever lost

This is the case this page was written for: the service account was deleted in GCP, and with it the ability to send. Recovering is the setup above run again, but two facts make it far less alarming than it looks, and both are worth internalising before you start:

- **The identity is deterministic.** A service account's email is `<name>@<project>.iam.gserviceaccount.com` — derived entirely from the name and the project. Recreate `joy-gmail-client` in `the-joy-496315` and you get back the *exact same* `client_email`: `joy-gmail-client@the-joy-496315.iam.gserviceaccount.com`. Because the app keys off that email and it is unchanged, **no application config changes** — you are only replacing the secret material inside the key file, not re-pointing anything.
- **Only two things are genuinely new.** The `private_key` / `private_key_id` in the JSON (a fresh key), and the numeric `client_id` (a new service account is a new OAuth2 client, so it gets a new ID). That new client ID is what forces the one Workspace step you can't skip.

So the recovery, concretely:

1. Recreate the service account with the **same name** in the **same project** (setup steps 2–3). Cut a new JSON key.
2. Drop the new key into place over the box's existing creds file (e.g. `joy-gmail-client-sa-creds.json`). Confirm `client_email` still matches what the app expects — it will, if the name and project match.
3. **Re-authorise domain-wide delegation with the *new* client ID** (setup steps 5–6). This is the step people forget: the old delegation row pointed at the *deleted* account's client ID and is now dead weight. The new account has a new client ID, and Workspace has never heard of it — so a send will fail with `unauthorized_client` until you add the new ID against the `gmail.send` scope. Delete the stale row if you like; add the new one regardless.
4. Verify with a live send (below).

The trap is assuming the credential file is the whole story. It isn't — the key proves *who* the account is, but Workspace delegation is what says that account is *allowed* to impersonate your users. Recreate the account and you've replaced the proof of identity but not the grant of permission; the grant has to be reissued because it named an ID that no longer exists.

## wiring it into the kernel

Two environment variables select and configure the hosted path (see `.env.example`):

| Variable | Meaning |
| --- | --- |
| `GMAIL_CREDENTIALS_FILE` | Path to the service-account JSON key on this box. Blank → the login code is written to `OTP.txt` instead of emailed. |
| `GMAIL_SENDER` | The Workspace mailbox the service account impersonates and sends as. Must be a real, deliverable user in the delegated domain. |

Both must be set for Gmail delivery; blank either and the box falls back to the file path. `main.py` reads them at startup and picks `GmailEmailClient` or `FileEmailClient` accordingly — there is no runtime toggle, the choice is made once at boot from config.

The key file must be caught by `.gitignore` and never committed — it is a live credential that can send mail as a real person. The existing patterns (`*-sa-creds.json`, `gmail-credentials*.json`, `*.gmail.json`) cover the conventional names; if you name a key something else, extend `.gitignore` first.

## propagation, and the 403 right after setup

Domain-wide delegation can take a few minutes to propagate through Google's side. A first send that returns `403` / `unauthorized_client` immediately after authorising is almost always just that — a short wait and a retry clears it. If it persists past a few minutes, the usual cause is a mismatch between the client ID in the delegation row and the one in the key file, or a scope typo — re-check step 6 against the `client_id` actually present in the JSON.

## verifying the chain

The test suite proves the identity state machine on the fake client and never touches Google, so a green suite says nothing about whether delivery works. Real delivery is verified by hand: trigger a `/login` for the sender's own address on the live box and confirm the code lands in the inbox. That single round trip exercises the entire chain — key on disk, delegation grant, impersonation, the `gmail.send` call — and is the only thing that proves the integration is actually back, as opposed to merely configured.
