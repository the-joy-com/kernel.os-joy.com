"""DTOs: validated shapes of data crossing the kernel's HTTP boundary.

A DTO only carries data—no behavior—just fields and their validation rules.
They're kept here, separate from main.py's routes, so a request's shape is defined once and a handler is focused on behavior.
Incoming DTOs live here. Outbound DTOs are the envelope (in main.py), and only get their own DTO class if a second route makes a generic worth it.
"""

from pydantic import BaseModel, Field


# Named for what it carries (a request) rather than the action,
# so it doesn't collide with the /intake route or the intake() handler.
class IntakeRequest(BaseModel):
    """One line captured at the shell's prompt, on its way into the kernel.

    A single typed line, never empty, capped so a stray paste can't ship an unbounded body.
    The shell trims before sending, so by the time a line gets here it already carries real text.
    """

    line: str = Field(min_length=1, max_length=4096)
