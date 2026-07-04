"""DTOs: validated shapes of data crossing the kernel's HTTP boundary.

A DTO only carries data—no behavior—just fields and their validation rules.
They're kept here, separate from main.py's routes,
so a request's shape is defined once and a handler is focused on behavior.
Incoming DTOs live here.
Outbound DTOs are the envelope (in main.py),
and only get their own DTO class if a second route makes a generic worth it.
"""

from pydantic import BaseModel, Field


# Named for what it carries (a request) rather than the action,
# so it doesn't collide with the /intake route or the intake() handler.
class IntakeRequest(BaseModel):
    """One line captured at the shell's prompt, on its way into the kernel.

    A single typed line, never empty,
    capped so a stray paste can't ship an unbounded body.
    The shell trims before sending,
    so by the time a line gets here it already carries real text.
    """

    line: str = Field(min_length=1, max_length=4096)
    # Which reply channel to nudge once this message has an answer,
    # if the browser registered one (see /push/subscribe).
    # Optional — a message with no channel still gets answered,
    # it just arrives with no one to notify.
    reply_channel_id: int | None = None


class LoginRequest(BaseModel):
    """An address asking to log in.

    The field is present-but-may-be-blank on purpose:
    a blank address is a valid *request shape* that simply matches no symbiot,
    so it gets the same canonical reply as everything else —
    never a 422 that would leak that blank is special.
    Capped at the practical maximum length of an email address.
    """

    address: str = Field(max_length=320)


class PushKeys(BaseModel):
    """The client key material a push payload is encrypted against.

    Both come straight from the browser's PushSubscription — p256dh is its public key, auth its secret —
    and together they let only that browser decrypt what the kernel sends.
    Capped well above their real base64url length as a stray-input guard.
    """

    auth: str = Field(min_length=1, max_length=256)
    p256dh: str = Field(min_length=1, max_length=256)


class PushSubscriptionRequest(BaseModel):
    """A browser registering where the kernel can push it a settled-message nudge.

    This mirrors a browser's PushSubscription exactly, so the shell forwards it as-is:
    the push-service endpoint, plus the keys a payload is encrypted against.
    The endpoint's length cap is generous — push-service URLs are long and opaque —
    just a guard against an unbounded body, not a real rule about length.
    """

    endpoint: str = Field(min_length=1, max_length=2048)
    keys: PushKeys


class SeenRequest(BaseModel):
    """The inbox message ids the shell is reporting it has shown to the symbiot.

    When the shell surfaces an inbox message it POSTs its id here, and the kernel marks it
    seen so /inbox stops offering it on the next open (see the /inbox/seen route).
    The list may be empty: the shell sometimes has nothing new to acknowledge, and that's a
    clean no-op rather than a validation error.
    It's capped so a stray client can't ship an unbounded array.
    """

    ids: list[int] = Field(max_length=1000)


class VerifyRequest(BaseModel):
    """A one-time code being spent for a session, and the address it was issued to.

    The address names whose live code this guess is charged against,
    so a wrong code burns an attempt on that symbiot rather than on no one —
    the kernel's brute-force budget.
    Like LoginRequest the address may be blank:
    a blank or unknown one simply matches no live code and gets the same "that code didn't work" reply,
    never a 422 that would single it out.
    """

    address: str = Field(max_length=320)
    code: str = Field(min_length=1, max_length=64)
