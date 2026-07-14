"""Message delivery behind a single interface.

The kernel depends on the *capability* to send a message, not on Gmail in particular —
so the identity flow is built and tested against this interface,
and the test suite swaps in a fake that records messages instead of putting them on the wire.
Two real deliveries live here, chosen per box (main.py picks between them by config):
GmailEmailClient puts the message on the wire and is the one piece that needs live credentials;
FileEmailClient writes it to a local file for a mailboxless box, needing nothing external at all.
Everything else is exercised without either.
"""

import base64
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol

from core import logs

# The narrowest scope that can send:
# it grants sending only, not reading the mailbox.
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"


class EmailClient(Protocol):
    """Anything that can send one message to one recipient."""

    def send(self, to: str, subject: str, body: str) -> None: ...


@dataclass
class SentMessage:
    to: str
    subject: str
    body: str


class FakeEmailClient:
    """Records what it was asked to send and never touches the network.

    Test-only:
    the suite reads `sent` to assert an email was (or wasn't) issued
    and to recover the login code that a real client would have delivered.
    """

    def __init__(self) -> None:
        self.sent: list[SentMessage] = []

    def send(self, to: str, subject: str, body: str) -> None:
        self.sent.append(SentMessage(to=to, subject=subject, body=body))


class FileEmailClient:
    """Writes the message to a local file instead of the wire — the mailboxless box's delivery.

    The kernel reaches for this when no Gmail is configured (main.py),
    so a box with nothing but a local Ollama can still stand up:
    login needs a code to reach a human, and here the human is the operator who owns the box,
    so the code reaching disk *is* it reaching them — the same trust root as the box's SSH key.

    Each send overwrites the file whole rather than appending:
    the identity flow keeps only one live code per symbiot (a fresh /login overwrites the last),
    so the file mirrors that — the newest code is the only one on disk,
    and a stale one is never left lying around to be mistaken for current.
    A console line signposts *that* a code was written and *where*, but never the code itself:
    the logs are a shared stream, the file is the private drop,
    and only the drop should ever hold the secret.
    """

    def __init__(self, path: str) -> None:
        self.path = path

    def send(self, to: str, subject: str, body: str) -> None:
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(f"To: {to}\nSubject: {subject}\n\n{body}\n")
        logs.get("identity").info(
            "no mailbox configured — login code for %s written to %s (read it there)", to, self.path
        )


class GmailEmailClient:
    """Sends through the Gmail API as a Workspace mailbox.

    Authenticated by a GCP service account with domain-wide delegation:
    the account holds no mailbox of its own,
    it *impersonates* `sender` (a real Workspace user) and sends as them.
    Credentials and the API client are built lazily on the first send,
    so importing this module — and running the test suite, which uses the fake — never needs the key.
    Without configuration it refuses loudly rather than pretend to send.
    """

    def __init__(self, credentials_file: str, sender: str) -> None:
        self.credentials_file = credentials_file
        self.sender = sender
        self._service = None

    def _gmail(self):
        if self._service is None:
            # Imported here, not at module top,
            # so the heavy Google libraries load only when mail is actually sent —
            # not in tests or at import time.
            from google.oauth2 import service_account
            from googleapiclient.discovery import build

            creds = service_account.Credentials.from_service_account_file(
                self.credentials_file, scopes=[GMAIL_SEND_SCOPE]
            ).with_subject(self.sender)
            self._service = build(
                "gmail", "v1", credentials=creds, cache_discovery=False
            )
        return self._service

    def send(self, to: str, subject: str, body: str) -> None:
        if not self.credentials_file or not self.sender:
            raise RuntimeError(
                "Gmail not configured: set GMAIL_CREDENTIALS_FILE and GMAIL_SENDER"
            )
        message = EmailMessage()
        message["To"] = to
        message["From"] = self.sender
        message["Subject"] = subject
        message.set_content(body)
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        self._gmail().users().messages().send(
            userId="me", body={"raw": raw}
        ).execute()
