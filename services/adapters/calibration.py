"""Measuring what the box underneath Ollama can actually afford to hold open.

Every other window in the catalog is a property of the *model*: the same figure on every machine,
decided by how far into its window a model still reads well, and owned by the code.
The local model's usable window is not like that.
Ollama allocates the whole window up front as a KV cache,
so the identical model holds 128K on a workstation and a fraction of that on a laptop,
and no amount of reading the weights will tell you which machine you are on.

Local-only is a mode real symbiots run, on hardware this project will never see —
the ones who want none of this on someone else's computer.
So the honest figure has to be found by measuring rather than by assuming,
and the machine adapts to the box instead of declaring a minimum spec and turning people away.

The measurement is deliberately Ollama's own accounting rather than the operating system's.
Ollama is reached over a URL that need not be localhost,
so probing this process's own hardware would be right on a single box
and quietly wrong the moment the model is served from another.
Ollama is in any case the only party that knows how it split the model
between the graphics card and main memory.
One load answers everything: `size` is what the model came to in total at a known window,
`size_vram` is how much of that the card took, and the difference is what spilled.
From those, the cost per token is arithmetic, and the ceiling is whatever the card was willing to hold.

The result is written to `model.measured_context_tokens` and never recomputed unless it is cleared,
so the probe is a one-time cost on a box rather than a toll on every boot.
"""

from dataclasses import dataclass

import ollama

from core import config
from core import logs
from services.adapters import models

log = logs.get("calibration")

# The window is a KV cache the box has to find room for,
# so a measurement landing under this is not a smaller window but an unusable one:
# the fixed head alone would fill it and leave no room for a diary fact or a line of conversation.
# Adapting downward stops here and says so.
MIN_USABLE_TOKENS = 8192


@dataclass(frozen=True)
class Measurement:
    """What one probe load found: the window the box can hold, and the arithmetic behind it.

    tokens is the answer — the window to adopt, already clamped and margined.
    The rest is kept so the decision can be read back rather than taken on trust,
    which is what /observe surfaces: a number with no derivation is not auditable."""

    tokens: int
    probe_tokens: int
    weights_bytes: int
    total_bytes: int
    vram_bytes: int
    bytes_per_token: float
    spilled: bool


def _loaded(client, model_name: str):
    """The running-model entry Ollama reports for `model_name`, or None if it isn't resident.

    Ollama names a loaded model by its fully-qualified tag,
    so a bare name and a tagged one have to be matched leniently rather than compared outright.
    """
    for entry in client.ps().models:
        name = entry.model or ""
        if name == model_name or name.split(":")[0] == model_name.split(":")[0]:
            return entry
    return None


def _system_available_bytes() -> int:
    """What main memory has free right now, read from /proc/meminfo.

    Only consulted when Ollama placed nothing on a graphics card —
    a box serving the model entirely on its processor, where main memory is the whole constraint.
    MemAvailable rather than MemFree,
    because the kernel's own figure already accounts for the reclaimable cache
    a fresh allocation would be handed."""
    with open("/proc/meminfo") as handle:
        for line in handle:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    return 0


def _weights_bytes(client, model_name: str) -> int:
    """The model's own resident size, before any window is opened on top of it.

    Read from Ollama's catalogue rather than the loaded entry, so it is the weights alone —
    the loaded entry's `size` already carries the KV cache this module is trying to isolate.
    """
    for entry in client.list().models:
        if (entry.model or "") == model_name:
            return int(entry.size or 0)
    return 0


def measure(model_name: str) -> Measurement | None:
    """Probe the box once and return the window it can hold for `model_name`, or None if it can't be read.

    The probe opens the window the catalog asks for and lets Ollama answer the question by placing the model.
    If the whole thing landed on the card, the box can afford what was asked and the catalog figure stands —
    that is the workstation case, and it shrinks nothing.
    If part of it spilled into main memory, the card's ceiling is exactly what it did take,
    and the affordable window is what fits under that ceiling once the weights and a margin are set aside.
    A box with no card at all is measured against main memory instead, on the same arithmetic.

    Returns None when the probe cannot be completed — Ollama unreachable, the model absent, the load refused.
    A failed probe deliberately does not invent a number:
    the caller keeps the catalog's figure and says so, which is a legible state,
    where a fabricated measurement would be a wrong one wearing a measurement's authority.
    """
    spec = models.spec(model_name)
    if spec is None:
        log.warning("calibration skipped: %r is not in the catalog", model_name)
        return None
    probe_tokens = spec.optimal_context_tokens
    client = ollama.Client(
        host=config.OLLAMA_BASE_URL, timeout=config.LLM_TIMEOUT_SECONDS
    )
    try:
        # A one-token answer: the reply is worthless, the *placement* is the measurement.
        client.generate(
            model=model_name,
            prompt="hi",
            stream=False,
            think=False,
            options={"num_ctx": probe_tokens, "num_predict": 1},
        )
        entry = _loaded(client, model_name)
        weights = _weights_bytes(client, model_name)
    except Exception as error:
        log.warning("calibration probe failed for %r: %s", model_name, error)
        return None
    if entry is None or not entry.size:
        log.warning("calibration probe left nothing resident for %r", model_name)
        return None

    total = int(entry.size)
    vram = int(entry.size_vram or 0)
    spilled = vram < total
    # Everything above the weights is the window's own cost — the KV cache and the buffers that ride with it —
    # and it scales with the window, so one load gives the per-token price.
    overhead = max(total - weights, 1)
    bytes_per_token = overhead / probe_tokens

    if not spilled:
        # It all fit where Ollama wanted it. The box can afford what the catalog asked for, so nothing shrinks.
        tokens = probe_tokens
    else:
        # The card took what it could and pushed the rest into main memory.
        # What it took *is* the ceiling — Ollama already made that judgment, and it knows the hardware.
        # A box with no card at all reports nothing resident there, so main memory is the ceiling instead.
        ceiling = vram if vram > 0 else _system_available_bytes()
        room = ceiling * (1 - config.LOCAL_WINDOW_MARGIN) - weights
        tokens = int(room / bytes_per_token) if room > 0 else 0
        tokens = max(MIN_USABLE_TOKENS, min(tokens, probe_tokens))

    if spilled and tokens == MIN_USABLE_TOKENS:
        log.warning(
            "%r fits only the minimum usable window (%d tokens) on this box — "
            "the fixed head leaves little room for memory in a reply",
            model_name,
            MIN_USABLE_TOKENS,
        )
    log.info(
        "%r measured at %d tokens (probed %d; %.1f GB total, %.1f GB on card, %.0f bytes/token)",
        model_name,
        tokens,
        probe_tokens,
        total / 1e9,
        vram / 1e9,
        bytes_per_token,
    )
    return Measurement(
        tokens=tokens,
        probe_tokens=probe_tokens,
        weights_bytes=weights,
        total_bytes=total,
        vram_bytes=vram,
        bytes_per_token=bytes_per_token,
        spilled=spilled,
    )
