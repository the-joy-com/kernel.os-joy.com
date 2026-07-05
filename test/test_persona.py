"""The persona is stored as two strings; this pins how they assemble.

No model reads the persona yet — these cover only the splice: the public voice with the
private half dropped into its slot, the graceful case where the private half is absent, and
the guarantee that the placeholder token never survives into the assembled voice either way.
The tests follow persona.PLACEHOLDER rather than hardcoding the token, so they stay true if
the token's spelling is ever changed. The files are pointed at temp paths so a test never
depends on what's actually on the box.
"""

from core import config
from services import persona


def _point_at(monkeypatch, public_body, private_body, tmp_path):
    # Write the public half, and the private half only when one is given; return with config
    # pointed at both. A None private_body leaves no private file on disk — the absent case.
    public = tmp_path / "public.md"
    public.write_text(public_body, encoding="utf-8")
    monkeypatch.setattr(config, "PERSONA_PUBLIC_FILE", str(public))
    private = tmp_path / "private.md"
    if private_body is not None:
        private.write_text(private_body, encoding="utf-8")
    monkeypatch.setattr(config, "PERSONA_PRIVATE_FILE", str(private))


def test_private_half_is_spliced_into_the_placeholder(tmp_path, monkeypatch):
    _point_at(
        monkeypatch,
        f"a partner with a spine.\n\n{persona.PLACEHOLDER}\n",
        "the symbiot's private context.",
        tmp_path,
    )
    voice = persona.load()
    assert "a partner with a spine." in voice  # the public frame is kept
    assert "the symbiot's private context." in voice  # the private half filled the slot


def test_absent_private_half_collapses_and_public_stands_alone(tmp_path, monkeypatch):
    _point_at(
        monkeypatch, f"a partner with a spine.\n\n{persona.PLACEHOLDER}\n", None, tmp_path
    )
    voice = persona.load()  # no private file on disk — must not raise
    assert "a partner with a spine." in voice


def test_placeholder_never_survives_assembly(tmp_path, monkeypatch):
    # Both ways: filled from a private half, and collapsed with none — no literal token left.
    _point_at(monkeypatch, f"voice.\n\n{persona.PLACEHOLDER}\n", "filled.", tmp_path)
    assert persona.PLACEHOLDER not in persona.load()
    _point_at(monkeypatch, f"voice.\n\n{persona.PLACEHOLDER}\n", None, tmp_path)
    assert persona.PLACEHOLDER not in persona.load()
