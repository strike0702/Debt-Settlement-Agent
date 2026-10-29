"""When the rep's speech over the agent counts as a barge-in (Phase 63 initial values).

Flux reports a start of turn for any speech, backchannels included, and Pipecat
would broadcast an interruption for each one. The pipeline turns that off
(``should_interrupt=False``) and asks ``is_barge`` instead, only while the agent
is speaking. Phase 65 tunes these constants with the harness; they are not
measured yet. A lone backchannel said while the agent is silent is still a turn
(the rep may be answering a yes/no read-back): that is decided elsewhere.
"""

from __future__ import annotations

import re

# Words that acknowledge without taking the floor. A phrase made only of these
# never barges ("okay, right" included); one other word makes it a real turn.
BACKCHANNELS: frozenset[str] = frozenset(
    {
        "mm", "mhm", "mmhm", "mm-hmm", "mmhmm", "hmm", "uh-huh", "uhhuh", "uh", "um",
        "okay", "ok", "right", "yeah", "yep", "yes", "sure", "alright", "got", "it",
        "i", "see", "go", "on",
    }
)  # fmt: skip
# Fewest words (backchannels excluded) before speech over the agent stops it.
MIN_BARGE_WORDS = 2

_WORD = re.compile(r"[a-z]+(?:-[a-z]+)*")


def words(text: str) -> list[str]:
    """Lower-case words of ``text`` (punctuation dropped)."""
    return _WORD.findall(text.lower())


def is_backchannel(text: str) -> bool:
    """True when every word of ``text`` is a backchannel (empty text is not one)."""
    ws = words(text)
    return bool(ws) and all(w in BACKCHANNELS for w in ws)


def is_barge(text: str) -> bool:
    """True when speech over the agent should stop it: enough non-backchannel words."""
    return sum(1 for w in words(text) if w not in BACKCHANNELS) >= MIN_BARGE_WORDS
