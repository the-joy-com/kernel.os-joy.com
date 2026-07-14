"""DTOs: validated shapes of data crossing the kernel's HTTP boundary.

A DTO only carries data—no behavior—just fields and their validation rules.
They're kept here, separate from main.py's routes,
so a request's shape is defined once and a handler is focused on behavior.
Incoming DTOs live here.
Outbound DTOs are the envelope (in main.py),
and only get their own DTO class if a second route makes a generic worth it.
"""

from typing import Literal

from pydantic import BaseModel, Field


class DeliveredRequest(BaseModel):
    """The message ids the shell is reporting it has shown the outcome of to the symbiot.

    After the shell renders an answer (or an abandonment) it read off /answers, it POSTs the id here,
    and the kernel stamps delivered_at — the reply's 'truly out' receipt,
    the honest counterpart on the way back to the outbox's COPY (see the /answers/delivered route).
    The list may be empty — the shell sometimes has nothing new to confirm —
    which is a clean no-op rather than a validation error.
    Capped so a stray client can't ship an unbounded array.
    """

    ids: list[int] = Field(max_length=1000)


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


class ModelConfigRequest(BaseModel):
    """One change to the model configuration — the body of POST /models.

    Three actions share the one route, told apart by `action`,
    because they are the one command's three verbs
    (the /models command drives all of them),
    and each returns the same full state so the shell re-renders from one source:
      - "register": add or edit an operator model. `name` is required;
        provider and the two window figures are optional
        and default to sensible local values (see model_config.upsert_model),
        so a bare name works.
      - "delete": remove an operator model. `name` is required.
      - "assign": point a role at a catalog model. `role` and `model` are required.
    The route validates the fields each action needs and surfaces a refusal
    (a builtin edited, a model in use, an unknown role)
    as a legible reason rather than a 422 —
    the fields are shape-valid, the *change* is what's refused.
    Everything is capped as a stray-input guard; real names and slugs are short.
    """

    action: Literal["register", "delete", "assign"]
    # register / delete: the model's own name (the exact id its provider answers to).
    name: str | None = Field(default=None, max_length=128)
    # register: the characteristics, all optional — an omitted one takes a sensible default.
    provider: str | None = Field(default=None, max_length=64)
    optimal_context_tokens: int | None = Field(default=None, ge=1, le=10_000_000)
    max_output_tokens: int | None = Field(default=None, ge=1, le=10_000_000)
    # assign: which role, and which catalog model to point it at.
    role: str | None = Field(default=None, max_length=64)
    model: str | None = Field(default=None, max_length=128)


class NotificationPreferenceRequest(BaseModel):
    """A symbiot flipping one notification channel on or off — the body of POST /notifications.

    channel names which one ('web_push', 'email');
    the route checks it against the channels that actually exist (notify.ALL_CHANNELS)
    and no-ops a name that isn't one,
    so an unknown slug can't write a phantom preference —
    the single source for the channel set stays in code, not duplicated as a rule here.
    enabled is the position: false to silence the channel, true to allow it again.
    Capped as a stray-input guard; a real slug is short.
    """

    channel: str = Field(min_length=1, max_length=64)
    enabled: bool


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

    When the shell surfaces an inbox message it POSTs its id here,
    and the kernel marks it seen so /inbox stops offering it on the next open
    (see the /inbox/seen route).
    The list may be empty: the shell sometimes has nothing new to acknowledge,
    and that's a clean no-op rather than a validation error.
    It's capped so a stray client can't ship an unbounded array.
    """

    ids: list[int] = Field(max_length=1000)


class TimezoneRequest(BaseModel):
    """A place the symbiot names, on its way to becoming the symbiot's stored local timezone.

    Free text on purpose — a city, a country, a casual "just landed in NYC" —
    because the kernel infers the IANA zone from it (services/zone.py),
    rather than asking the human to type an identifier.
    Never empty, and capped so a stray paste can't ship an unbounded body;
    the shell trims before sending.
    """

    location: str = Field(min_length=1, max_length=200)


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
