-- A model gains a window measured on the box, told apart from the window the code believes in.
--
-- optimal_context_tokens is a *judgment*: the model's effective window,
-- held below its advertised maximum because recall frays across the back half of a window with no error to show for it.
-- It is a property of the model, identical on every box, and the code owns it —
-- reconcile_and_seed overwrites it from BUILTIN_MODELS on every boot precisely so a builtin can't drift.
--
-- That is the wrong shape for a local model,
-- whose usable window is not a property of the model at all but of the machine underneath it.
-- Ollama allocates the whole window up front as a KV cache,
-- so the same model that holds 128K on a workstation holds a fraction of it on a laptop,
-- and the honest figure can only be found by measuring the box rather than by reading the weights.
-- Writing that measurement into optimal_context_tokens would put it directly in the path of the boot-time overwrite that exists to keep builtins honest,
-- and the next restart would erase it.
--
-- So the measurement lands in its own column, which reconcile_and_seed never touches.
-- NULL is the ordinary state and means "never measured, use the code's judgment" —
-- true of every cloud model, which has no local memory cost to measure and whose window the provider fixes anyway.
-- The resolver reads COALESCE(measured_context_tokens, optimal_context_tokens),
-- so a measured box uses its own figure and an unmeasured one is unchanged.
--
-- The CHECK keeps a measurement positive: zero or negative is not a smaller window, it is a broken probe,
-- and it should fail at the write rather than resolve into a budget that can hold nothing.
ALTER TABLE model
    ADD COLUMN measured_context_tokens INTEGER,
    ADD CONSTRAINT model_measured_context_tokens_positive
        CHECK (measured_context_tokens IS NULL OR measured_context_tokens > 0);
