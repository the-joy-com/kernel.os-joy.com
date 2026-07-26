# google integration: generation on the Agent Platform, and login codes through Gmail

The kernel depends on Google in two unrelated places, and it is worth holding them apart from the first sentence. **Generation** runs on the **Agent Platform**: every judgment the router makes and every reply the machine composes goes out over the top rung of the generative ladder, so this one is live on essentially every request. **Login-code delivery** calls the **Gmail API** to put a code in a symbiot's inbox, which happens only when someone logs in. They share exactly one thing — a GCP project — and are otherwise independent: different service accounts, different keys, different failure modes. Either can be wired without the other.

What they *do* share is a shape, and it's the shape this page is really about: both authenticate as a **service account with a JSON key on the box**, never as a human and never with an API key. This document is the operational reference for standing each one up, wiring it into a box, and — the reason it exists as its own page — rebuilding it from nothing the day a credential is lost.

The code these moves sit behind is [`services/adapters/llm.py`](../services/adapters/llm.py) (`_google`) for generation and [`services/adapters/email_client.py`](../services/adapters/email_client.py) (`GmailEmailClient`) for delivery; this page is the other half, the moves in Google's consoles that both assume have already happened.

## generation: the Gemini rung on the Agent Platform

The generative top rung calls Gemini through the **Agent Platform** — Google's enterprise surface, with its zero-retention terms — and reaches it over **application-default credentials**, never an API key. That is not a stylistic preference: a bare API key routes to the consumer Developer API instead, a different product with different data-governance terms, so the key path is not a shortcut to the same place. `_google` builds its client with `vertexai=True` and no explicit credentials, which means google-auth resolves the identity itself, from the environment, at call time.

Two environment variables select the rung, and one selects the credential. `GOOGLE_CLOUD_PROJECT` names the project the calls bill to — leave it empty and the rung is simply not wired, so every call falls straight through to Scaleway. `GOOGLE_CLOUD_LOCATION` names the region (`global` is what The Joy runs). `GOOGLE_APPLICATION_CREDENTIALS` points at the service-account key, and is read by google-auth directly rather than by `core/config.py`, which is why it appears in `.env` but nowhere in the kernel's own configuration — `load_dotenv()` puts it in the process environment and the library finds it there, ahead of anything `gcloud` has left on disk.

### the credential has to be a service account

`gcloud auth application-default login` will make the rung work, and on a dev box that is fine. It must not be what holds a server open, and the reason is worth stating plainly because the failure is silent.

That command mints a **human's** credential: a refresh token tied to a person's Google identity, which Google expires on a policy schedule by design. When it lapses, the kernel does not break. An unresolvable credential is outage-class, so `_google` raises `_Outage` and the ladder falls through to Scaleway exactly as designed — the machine keeps answering, in a voice nobody can tell apart, and nothing surfaces anywhere. So the symptom is not downtime; it is the switch quietly undoing itself, paying the rung below's prices and earning none of the cached-input economics the move to Google exists for, discoverable weeks later on a bill.

A service account has no expiring refresh token. Its key signs for a fresh access token on every call, indefinitely, with nobody at the keyboard. That is the whole difference, and it is the difference between a rung that holds and a rung with a human's expiry date stapled to it.

### standing the account up

The account needs Agent Platform inference on the project and nothing else — `roles/aiplatform.expressUser` is sufficient, and is what The Joy runs. Three commands, from any machine with an authenticated `gcloud` — your laptop is fine, and the box these run *from* has nothing to do with the box the kernel runs *on*:

```bash
gcloud iam service-accounts create joy-agent-platform --project=<project-id>
gcloud projects add-iam-policy-binding <project-id> \
  --member=serviceAccount:joy-agent-platform@<project-id>.iam.gserviceaccount.com \
  --role=roles/aiplatform.expressUser
gcloud iam service-accounts keys create joy-agent-platform-sa-creds.json \
  --iam-account=joy-agent-platform@<project-id>.iam.gserviceaccount.com
```

Place the key in the clone and name it to match the `*-sa-creds.json` pattern in `.gitignore`, then set `GOOGLE_APPLICATION_CREDENTIALS` to its **absolute** path. Absolute, not relative: a relative path resolves against the process's working directory, which is the repo root under the systemd unit but is not guaranteed to be anywhere in particular when a script is run by hand.

Cut a separate key per box rather than copying one file around. A key is revocable on its own, so a compromised server costs you that key and not the dev box's as well.

### the key never arrives by deploy

`deploy.sh` is a `git pull`, and the key is gitignored — so it cannot travel that way, by design. A new box needs the key placed by hand and the three variables added to its own `.env`, which likewise lives only on that box. Everything else the switch needs does ride the deploy: `uv sync --frozen` installs the `google-genai` SDK from the lockfile, migrations run at startup, and the model catalog seeds itself from code on boot.

The kernel never shells out to `gcloud`, so a serving box needs the CLI neither installed nor authenticated — placing the key is the entire credential setup, and a deploy is a `git pull` and a restart. What the box does need is for the service user to be able to read that key: it is a private key, so it wants `chmod 600` and the service user as its owner, and a key copied in as one user under a unit running as another is the failure that looks like a credential problem and is not one. Nothing about the credential expires on a schedule after that; only revoking the key, or removing the account's role, takes the rung down.

One thing to check on a box that ran an earlier build: if its `.env` pins a role at an old model name — a `REPLY_MODEL` left pointing at a Scaleway id, say — that override silently wins over the Gemini defaults and the top rung never answers for that role.

### verifying the chain

The pytest suite fakes the google-genai boundary, so a green suite proves the ladder's logic and says nothing about whether the credential works. Real verification is the by-hand smoke, [`test/qa/0012_gemini_smoke.py`](../test/qa/0012_gemini_smoke.py), which writes nothing to the database and makes a handful of tiny billable calls. It proves the four things only a live run can: that the credential authenticates against the Agent Platform, that each of the three tier model ids is real and answers, that structured output binds to a Pydantic schema, and — the number the cost case turns on — what Google's own tokenizer makes of the fixed cacheable head. Run it on the server as the service user after a deploy, not just locally; the point is to prove the credential works as *that* identity.

## login codes: the Gmail send path

The rest of this page is the delivery integration, which is entirely separate from the rung above.

## what the Gmail integration actually is

The Gmail client authenticates as a **GCP service account with domain-wide delegation** — a headless machine identity, no interactive login, ideal for a server that must send mail at 3am with no human present. The subtlety worth holding onto: the service account **holds no mailbox of its own**. It doesn't send *as itself*. Domain-wide delegation lets it **impersonate a real Workspace user** — the address in `GMAIL_SENDER` — and send as them, so the mail arrives from a person the recipient recognises, not from a robot account.

Three properties fall out of that design, and each is deliberate:

- **Send-only, by scope.** The only authority ever granted is `https://www.googleapis.com/auth/gmail.send`. The service account cannot read the impersonated mailbox, cannot list it, cannot delete from it. If the key leaks, the blast radius is "someone can send mail as the sender", not "someone can read the sender's mail".
- **It refuses rather than pretends.** Until both `GMAIL_CREDENTIALS_FILE` and `GMAIL_SENDER` are set, the client raises instead of silently no-op'ing. A box that thinks it can email but can't will fail loudly on the first send, not swallow a login code.
- **The key is per-box and never committed.** Each box carries its own JSON key on disk, matched by `.gitignore` (`*-sa-creds.json`, `gmail-credentials*.json`, `*.gmail.json`). The Google libraries and the key load lazily on the first send, so import and the test suite never touch either — the suite runs entirely on the fake client.

If a box has no Workspace at all — a home server, a fully-local setup — **none of this applies**: leave both env vars blank and the login code is written to `OTP.txt` instead of emailed. See the kernel README's "Running fully local" section. Everything below is only for a box that genuinely sends.

## the Gmail prerequisites

Before touching a console, three things have to be true:

- **A GCP project** that will own the service account. The Gmail API must be *enabled* in the Google project (APIs & Services → Library → "Gmail API" → Enable).
- **Google Workspace admin access** on the sender's domain. Domain-wide delegation is authorised in the Workspace admin console, and only an admin can do it — a plain project owner cannot.
- **A real sender mailbox** in that domain — an actual live Workspace user the service account will impersonate. This becomes `GMAIL_SENDER`. It must be a deliverable account, not an alias or a placeholder.

## the Gmail setup, end to end

The order matters: GCP first, because the Workspace step needs the numeric client ID that only exists once the service account is made.

### in the GCP console for your project

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

## if the Gmail service account is ever lost

This is the case this page was written for: the service account was deleted in GCP, and with it the ability to send. Recovering is the setup above run again, but two facts make it far less alarming than it looks, and both are worth internalising before you start:

- **The identity is deterministic.** A service account's email is `<name>@<project>.iam.gserviceaccount.com` — derived entirely from the name and the project. Recreate `joy-gmail-client` in the Google project and you get back the *exact same* `client_email`. Because the app keys off that email and it is unchanged, **no application config changes** — you are only replacing the secret material inside the key file, not re-pointing anything.
- **Only two things are genuinely new.** The `private_key` / `private_key_id` in the JSON (a fresh key), and the numeric `client_id` (a new service account is a new OAuth2 client, so it gets a new ID). That new client ID is what forces the one Workspace step you can't skip.

So the recovery, concretely:

1. Recreate the service account with the **same name** in the **same project** (setup steps 2–3). Cut a new JSON key.
2. Drop the new key into place over the box's existing creds file (e.g. `joy-gmail-client-sa-creds.json`). Confirm `client_email` still matches what the app expects — it will, if the name and project match.
3. **Re-authorise domain-wide delegation with the *new* client ID** (setup steps 5–6). This is the step people forget: the old delegation row pointed at the *deleted* account's client ID and is now dead weight. The new account has a new client ID, and Workspace has never heard of it — so a send will fail with `unauthorized_client` until you add the new ID against the `gmail.send` scope. Delete the stale row if you like; add the new one regardless.
4. Verify with a live send (below).

The trap is assuming the credential file is the whole story. It isn't — the key proves *who* the account is, but Workspace delegation is what says that account is *allowed* to impersonate your users. Recreate the account and you've replaced the proof of identity but not the grant of permission; the grant has to be reissued because it named an ID that no longer exists.

## wiring Gmail into the kernel

Two environment variables select and configure the hosted path (see `.env.example`):

| Variable | Meaning |
| --- | --- |
| `GMAIL_CREDENTIALS_FILE` | Path to the service-account JSON key on this box. Blank → the login code is written to `OTP.txt` instead of emailed. |
| `GMAIL_SENDER` | The Workspace mailbox the service account impersonates and sends as. Must be a real, deliverable user in the delegated domain. |

Both must be set for Gmail delivery; blank either and the box falls back to the file path. `main.py` reads them at startup and picks `GmailEmailClient` or `FileEmailClient` accordingly — there is no runtime toggle, the choice is made once at boot from config.

The key file must be caught by `.gitignore` and never committed — it is a live credential that can send mail as a real person. The existing patterns (`*-sa-creds.json`, `gmail-credentials*.json`, `*.gmail.json`) cover the conventional names; if you name a key something else, extend `.gitignore` first.

## propagation, and the 403 right after setup

Domain-wide delegation can take a few minutes to propagate through Google's side. A first send that returns `403` / `unauthorized_client` immediately after authorising is almost always just that — a short wait and a retry clears it. If it persists past a few minutes, the usual cause is a mismatch between the client ID in the delegation row and the one in the key file, or a scope typo — re-check step 6 against the `client_id` actually present in the JSON.

## verifying the Gmail chain

The test suite proves the identity state machine on the fake client and never touches Google, so a green suite says nothing about whether delivery works. Real delivery is verified by hand: trigger a `/login` for the sender's own address on the live box and confirm the code lands in the inbox. That single round trip exercises the entire chain — key on disk, delegation grant, impersonation, the `gmail.send` call — and is the only thing that proves the integration is actually back, as opposed to merely configured.
