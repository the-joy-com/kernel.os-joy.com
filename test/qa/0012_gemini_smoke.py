"""By-hand smoke test: the Google (Gemini) top rung — live, against the Agent Platform over ADC.

The pytest suite (test/test_llm.py) proves the ladder's *logic* with the google-genai boundary faked:
that Google is tried first,
that an outage falls through to Scaleway,
that a 4xx surfaces,
that an unwired project is skipped,
and that the Scaleway and Mistral fallbacks resolve per tier.
It never proves the things only a live run can, because a fake can't:
  - that ADC actually authenticates against the Agent Platform (vertexai=True),
    with no API key anywhere;
  - that each of the three Gemini model ids we ship is real and answers —
    the flagship still a preview id;
  - that a bounded call comes back with thinking held at its floor
    (a 3.x model can't turn it off);
  - that structured output binds to a Pydantic schema and validates;
  - and, the one number the whole cost case turns on,
    whether the fixed cacheable head clears the 4096-token implicit-caching floor
    *on Google's own tokenizer*, not our o200k estimate.
This script is that other half.

It is a pure generation smoke — it writes nothing to the database,
so there is no transaction to roll back.

    GOOGLE_CLOUD_PROJECT=<your-project-id> python test/qa/0012_gemini_smoke.py
    python test/qa/0012_gemini_smoke.py --project <your-project-id> --location global

Prerequisites (see doc/google-integration.md and README, "Models"):
  - A resolvable application-default credential, with the Agent Platform API enabled on the project.
    On a server that means a service-account key, with GOOGLE_APPLICATION_CREDENTIALS
    pointing at its absolute path in .env — never an interactive `gcloud auth application-default login`,
    whose refresh token Google expires on a schedule
    (when it lapses the rung fails outage-class and the ladder falls silently to Scaleway).
    No API key is used or wanted: that path is the consumer Developer API, not the enterprise one.
  - A project id, from GOOGLE_CLOUD_PROJECT (as the kernel reads it) or the --project flag here.
  - Network reach to the Agent Platform.
    This makes real, billable Gemini calls — a handful of tiny ones.
"""

import argparse
import os
import sys

# Direct-run from anywhere: put the repo root on the path so `core`/`services` import cleanly.
# This file sits at test/qa/, so the repo root is three directories up.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from pydantic import BaseModel

from core import config
from services.adapters import llm, models
from services.loop import persona

# The three tier ids the switch ships — the primaries the roles resolve to,
# each proven here to be real and to answer.
TIER_MODELS = [
    ("flagship", config.FLAGSHIP_MODEL),
    ("mid", config.MID_MODEL),
    ("small", config.SMALL_MODEL),
]

# The implicit-caching floor on the 3.x models: the fixed head must clear this to cache at all.
CACHE_FLOOR = 4096


class _Ping(BaseModel):
    """A tiny structured-output shape, to prove the schema binds and validates on the Agent Platform."""

    status: str
    number: int


def _require_project(args) -> None:
    project = args.project or config.GOOGLE_CLOUD_PROJECT
    if not project:
        sys.exit(
            "no project set. Pass --project <id>, or export GOOGLE_CLOUD_PROJECT "
            "(the same var the kernel reads), then re-run."
        )
    # config is read once at import; point the boundary at the chosen project/location for this run.
    config.GOOGLE_CLOUD_PROJECT = project
    if args.location:
        config.GOOGLE_CLOUD_LOCATION = args.location
    print(f"project : {config.GOOGLE_CLOUD_PROJECT}   location : {config.GOOGLE_CLOUD_LOCATION}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", help="GCP project id (overrides GOOGLE_CLOUD_PROJECT for this run)")
    parser.add_argument("--location", help="Agent Platform location (overrides GOOGLE_CLOUD_LOCATION)")
    args = parser.parse_args()

    _require_project(args)

    # --- 1. each tier's Gemini id is real and answers, live -------------------------------------
    # The output cap is generous on purpose:
    # reasoning cannot be turned off on a 3.x model, only down,
    # and its thinking tokens are charged against max_output_tokens —
    # so a tight cap is spent entirely on the hidden reasoning and leaves nothing for visible text.
    # The real kernel calls carry the model's own 16384 ceiling, so this is a smoke concern only;
    # 512 is ample for a one-word reply plus its thinking.
    print("\n=== 1. the three tier models answer (direct _google calls) ===")
    for tier, model in TIER_MODELS:
        reply = llm._google(model, "Reply with exactly one word: online.", None, 0, 512)
        assert reply and reply.strip(), f"{model} came back empty"
        print(f"  ✓ {tier:8} {model:26} → {reply.strip()[:60]!r}")

    # --- 2. structured output binds to a schema and validates ----------------------------------
    print("\n=== 2. structured output on the Agent Platform ===")
    raw = llm._google(
        config.MID_MODEL,
        "Return JSON: set status to the word ok, and number to 7.",
        _Ping,
        0,
        1024,  # generous, so thinking (counted against this cap) leaves room for the JSON
    )
    ping = _Ping.model_validate_json(raw)
    print(f"  ✓ validated {ping!r} from {config.MID_MODEL}")
    assert ping.status and isinstance(ping.number, int), "the structured reply did not validate as expected"

    # --- 3. the whole ladder answers through the top rung --------------------------------------
    # With a project wired, the role resolves straight to its Gemini primary,
    # so in the steady state this reply is Gemini's — Scaleway and the rungs beneath only catch an outage.
    # (Which rung answered isn't in the return value;
    # the direct calls above are what prove Gemini specifically.
    # This proves the real entry point runs.)
    print("\n=== 3. the full generate() ladder, top rung live ===")
    spoken = llm.generate("In one short sentence, say something a cyberpunk cyberware would say on boot.")
    print(f"  reply : {spoken.strip()}")
    assert spoken and spoken.strip(), "the ladder returned an empty reply"
    print("  ✓ the ladder answered — read the line above to judge the voice")

    # --- 4. the cached head, measured on Google's own tokenizer --------------------------------
    # The number the cost case turns on:
    # the fixed head (preamble + persona) must clear the 4096 floor for implicit caching to fire on a 3.x model.
    # Our o200k estimate is a proxy; this is the real count.
    print("\n=== 4. the cacheable head vs the 4096 implicit-caching floor ===")
    from google import genai  # local import: only this section needs the raw client

    client = genai.Client(
        vertexai=True, project=config.GOOGLE_CLOUD_PROJECT, location=config.GOOGLE_CLOUD_LOCATION
    )
    head = persona.head()
    print(f"  our o200k estimate of the head : {models.count_tokens(head)}")
    for tier, model in TIER_MODELS:
        counted = client.models.count_tokens(model=model, contents=head).total_tokens
        verdict = "clears" if counted >= CACHE_FLOOR else "SHORT of"
        print(f"  {model:26} counts the head at {counted:5} tokens — {verdict} the {CACHE_FLOOR} floor")
    print(
        "\n  Note: the reply's cached prefix is the head plus its small framing,\n"
        "  so a head that clears the floor here clears it comfortably in situ;\n"
        "  one that is short is the signal to grow the persona further\n"
        "  or move to explicit CachedContent."
    )

    print("\nsmoke run complete — no writes were made.")


if __name__ == "__main__":
    main()
