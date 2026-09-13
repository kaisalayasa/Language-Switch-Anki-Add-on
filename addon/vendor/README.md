# Vendored third-party code

Anki addons run inside Anki's own bundled Python interpreter, with no `pip` available at
runtime, and `claude.md`'s hard constraints rule out any network call except the Piper
binary/voice downloads. A dependency that isn't a compiled/platform-specific wheel (which
would have to be downloaded and built per-OS/arch, the exact problem the Piper *subprocess*
design exists to avoid — see `addon/tts/piper_binary_manager.py`) is vendored here as plain
source instead.

Unlike `addon/user_files/`, this directory **is** committed to git — it's part of the addon,
not per-machine data.

## `langdetect/` — 1.0.9, from PyPI

Used by `addon/core/language_detect.py` (M4) as the Latin-script disambiguation fallback
behind the Unicode-script fast pass. Chosen over `claude.md`'s literally-named `py3langid`
because that pulls in `numpy` (a compiled, per-platform dependency) — `langdetect` is pure
Python and needs only `six`. License: **Apache License 2.0** (verified from the package's own
`LICENSE`/`NOTICE` — its `setup.py` metadata field says `MIT`, which is simply wrong; go by
the actual license file). `LICENSE` and `NOTICE` are kept alongside the code for attribution.
`langdetect/tests/` was dropped — not needed at runtime.

Its `profiles/` subdirectory (55 files, ~2.3MB) is real per-language n-gram data read from
disk at detection time (`langdetect/detector_factory.py`'s `PROFILES_DIRECTORY`, computed
relative to its own `__file__`) — not fetched over the network, so vendoring it whole is what
makes offline detection work at all.

`addon/core/language_detect.py` sets `langdetect.DetectorFactory.seed = 0` before use.
Without it, `detect_langs()` seeds Python's own `random` from OS entropy per call, making the
same field text produce a different guess on different runs — confirmed empirically, not a
theoretical concern.

## `six.py` — 1.17.0, from PyPI

`langdetect`'s only real dependency (`six.moves.xrange/zip`, `six.u()`, `six.iteritems()`,
`six.print_` — all trivial shims on Python 3, this project's only target). License: MIT
(header preserved at the top of the file).

## How it's imported

`addon/core/language_detect.py` prepends this directory to `sys.path` before importing
`langdetect`, computed from its own `__file__` so it works the same way whether loaded by
real Anki or by `python -m unittest discover -s tests -t .`. No other module needs to know
this directory exists.
