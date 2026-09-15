"""Local LLM support (Qwen2.5-1.5B-Instruct via a llama.cpp subprocess). No ``anki``/``aqt``
imports anywhere in this package -- see ``tests/test_purity.py``.

Unlike ``addon/core``, this package is **not** required to be language-agnostic: describing
languages and Anki template syntax to the model is its entire job. ``prompt.py`` is explicitly
exempt from the no-language-name rule that applies to ``addon/core``.
"""
