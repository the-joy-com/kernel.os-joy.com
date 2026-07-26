"""Calibration: measuring the window the box underneath Ollama can actually hold.

Everything here runs with the Ollama client faked at its own boundary,
so a passing suite proves the arithmetic and the clamps — what a probe concludes from what it observed —
without loading a model or touching a graphics card.
The live half, that a real box measures to a sane figure, is proven by hand.

The store-side tests do use the test database, because the whole point of the measured column
is that it survives the boot-time reconcile that rewrites builtin rows from code.
"""

from types import SimpleNamespace

from core import db
from services.adapters import calibration, llm, models
from services.memory import model_config

# The probe loads whatever the catalog asks for, so a fake has to agree with the catalog to be read.
_LOCAL = "qwen3.5:4b"
_WEIGHTS = 3_400_000_000


class _FakeOllamaClient:
    """Stand-in for ollama.Client as calibration uses it: generate to force a load, then ps and list to read it.

    total and vram are what the fake reports the load came to, which is the whole input to the arithmetic.
    A generate that raises stands for a box that couldn't complete the probe at all.
    """

    def __init__(self, *, total: int, vram: int, weights: int = _WEIGHTS, raises: Exception | None = None):
        self._total = total
        self._vram = vram
        self._weights = weights
        self._raises = raises
        self.captured = {}

    def __call__(self, *, host=None, timeout=None):
        return self

    def generate(self, **kwargs):
        self.captured["generate"] = kwargs
        if self._raises is not None:
            raise self._raises
        return SimpleNamespace(response="ok")

    def list(self):
        return SimpleNamespace(models=[SimpleNamespace(model=_LOCAL, size=self._weights)])

    def ps(self):
        return SimpleNamespace(
            models=[SimpleNamespace(model=_LOCAL, size=self._total, size_vram=self._vram)]
        )


def _probe(monkeypatch, **kwargs) -> calibration.Measurement | None:
    fake = _FakeOllamaClient(**kwargs)
    monkeypatch.setattr(calibration.ollama, "Client", fake)
    return calibration.measure(_LOCAL)


def test_a_box_that_holds_it_all_keeps_the_catalog_window(client, monkeypatch):
    # The workstation case: nothing spilled, so the box affords what the catalog asked for and nothing shrinks.
    # This is the branch that keeps calibration from punishing good hardware.
    measurement = _probe(monkeypatch, total=12_000_000_000, vram=12_000_000_000)

    assert measurement is not None
    assert measurement.spilled is False
    assert measurement.tokens == models.spec(_LOCAL).optimal_context_tokens


def test_a_spill_shrinks_the_window_to_what_the_card_took(client, monkeypatch):
    # The laptop case. The card took 8 GB of a 12.8 GB load, so 8 GB is the ceiling it was willing to hold,
    # and the window is what fits under it once the weights and the margin are set aside.
    total, vram = 12_800_000_000, 8_000_000_000
    measurement = _probe(monkeypatch, total=total, vram=vram)

    assert measurement is not None and measurement.spilled is True
    probe = models.spec(_LOCAL).optimal_context_tokens
    # The arithmetic restated rather than re-derived from the module, so a change of formula fails this.
    bytes_per_token = (total - _WEIGHTS) / probe
    room = vram * 0.9 - _WEIGHTS
    assert measurement.tokens == int(room / bytes_per_token)
    assert measurement.tokens < probe  # it genuinely shrank


def test_a_box_too_small_lands_on_the_minimum_rather_than_an_unusable_window(client, monkeypatch):
    # A card that took almost nothing would compute a window smaller than the fixed head.
    # Adapting downward has a floor: below it the mode cannot work, so it clamps and warns rather than
    # resolving into a budget that could not hold the persona, let alone a memory.
    measurement = _probe(monkeypatch, total=12_800_000_000, vram=3_500_000_000)

    assert measurement is not None
    assert measurement.tokens == calibration.MIN_USABLE_TOKENS


def test_a_failed_probe_invents_no_number(client, monkeypatch):
    # Ollama unreachable, model absent, load refused — all the same answer.
    # None is a legible state the caller can act on; a made-up figure would be a wrong one
    # wearing a measurement's authority.
    assert _probe(monkeypatch, total=0, vram=0, raises=RuntimeError("no such model")) is None


def test_the_probe_opens_the_catalog_window_to_measure_it(client, monkeypatch):
    # The measurement is only meaningful against a known window, so the probe must name one.
    fake = _FakeOllamaClient(total=12_800_000_000, vram=8_000_000_000)
    monkeypatch.setattr(calibration.ollama, "Client", fake)
    calibration.measure(_LOCAL)

    assert fake.captured["generate"]["options"]["num_ctx"] == models.spec(_LOCAL).optimal_context_tokens
    assert fake.captured["generate"]["options"]["num_predict"] == 1  # the reply is worthless; the placement isn't


def test_a_measured_window_wins_over_the_code_and_survives_reconcile(client):
    # The whole reason the measurement has its own column. reconcile_and_seed rewrites a builtin's
    # optimal_context_tokens from code on every boot, by design — and must leave the measured figure alone,
    # or a box would re-measure every restart and never keep what it learned.
    with db.get_pool().connection() as conn:
        model_config.set_measured_window(conn, _LOCAL, 43_235)
        model_config.reconcile_and_seed(conn)
        models.reload_from_conn(conn)

        assert model_config.measured_window(conn, _LOCAL) == 43_235
        assert models.spec(_LOCAL).optimal_context_tokens == 43_235  # the resolver reads the measurement
        row = next(m for m in model_config.catalog(conn) if m["name"] == _LOCAL)
        # Both figures stay readable side by side, so a shrunk window is legible as a decision, not a mystery.
        assert row["measured_context_tokens"] == 43_235
        assert row["optimal_context_tokens"] == models.BUILTIN_MODELS[_LOCAL].optimal_context_tokens

        model_config.set_measured_window(conn, _LOCAL, None)  # clearing puts the code's judgment back
        models.reload_from_conn(conn)
        assert models.spec(_LOCAL).optimal_context_tokens == models.BUILTIN_MODELS[_LOCAL].optimal_context_tokens


def test_local_mode_refits_the_prompt_for_the_model_that_answers(client, monkeypatch):
    # A prompt is fitted upstream to the *requested* model's window — a cloud id on a local-only box.
    # Down the cloud ladder that mismatch is safe, because the floor is the widest rung.
    # Here it is not: a measured box can hold far less than the cloud model the prompt was fitted for,
    # so the local branch has to fit again, against the model actually about to answer.
    monkeypatch.setenv("IS_LOCAL", "1")
    fitted = []
    monkeypatch.setattr(llm, "_fit", lambda prompt, context, model: fitted.append(model) or prompt)
    monkeypatch.setattr(llm, "_ollama", lambda *a, **k: "local answered")

    assert llm.generate("hello", context="some facts") == "local answered"

    # Twice: once upstream for the requested cloud model, once here for the local model that replaces it.
    assert fitted[0] == llm.models.role_name("rerank")
    assert fitted[-1] == llm.config.GENERATIVE_LOCAL_FALLBACK_MODEL
