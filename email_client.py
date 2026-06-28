"""Email delivery behind a single interface.

The kernel depends on the *capability* to send a message, not on Gmail in particular —
so the identity flow is built and tested against this interface,
and the test suite swaps in a fake that records messages instead of putting them on the wire.
Real delivery (GmailEmailClient) is the one piece that needs live credentials;
everything else is exercised without them.
"""

import base64
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol

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
