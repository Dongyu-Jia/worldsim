"""WorldSim: a small local world simulation.

A world has a schema (typed fields), a state (values for every field plus text: a narrative, key questions and
attributes), actors (role, what they care about, an action space, private information) and a synthesizer. One
iteration calls every enabled actor in parallel, then the synthesizer, validates its answer against the schema and
saves the next state. LLM calls go to OpenRouter with the user's key, or to a deterministic mock without a key.

    python -m worldsim                  # http://127.0.0.1:8940  (WORLDSIM_PORT to change)
    WORLDSIM_LLM=mock python -m worldsim

See docs/WORLDSIM.md.
"""

__version__ = "0.1.0"
