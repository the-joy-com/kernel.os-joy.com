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


class LoginRequest(BaseModel):
    """An address asking to log in.

    The field is present-but-may-be-blank on purpose:
    a blank address is a valid *request shape* that simply matches no symbiot,
    so it gets the same canonical reply as everything else —
    never a 422 that would leak that blank is special.
    Capped at the practical maximum length of an email address.
    """

    address: str = Field(max_length=320)


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
