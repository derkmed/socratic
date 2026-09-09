"""The learner profile: one ledger, one narrative, one watermark (D8, ADR-0008).

A profile has two parts. The **ledger** is structured and exactly recomputed -
topic counts, mode history, weak-area tallies, outcome counts - incremented from
the attempts since the last watermark, so its figures are arithmetic and cannot
drift. The **narrative** is the LLM-authored prose summary, folded forward
incrementally. Where the two disagree **the ledger is authoritative**, which is
why `render_profile` puts it first. The **watermark** is the last attempt
`ProfileBuilder` processed, so each run reads only what is new.

**Its own module, deliberately.** `records.py` is the persisted *attempt* record
shape and `prompting.py` is the prompt assembler; the profile is neither, and it
is needed by both. Defining it in `records.py` would make the assembler import
the attempt records, and defining it in `prompting.py` would make the
persistence ports import the assembler - backwards, since a port should not
depend on a renderer. A module of its own leaves both dependencies pointing at a
leaf that imports nothing from the domain at all. There were two classes of this
name before, one per module, and neither could be handed to the other's code
(#36).

The domain never reads the profile's internals (ADR-0006): it reaches a prompt
only through `prompting.render_profile`, and `tests/test_prompting.py` scans the
source for any other module reading the ledger or the narrative off a profile.
(The scan is a line-level regex, so it reads this sentence too - hence the
circumlocution.) The shape here is therefore free to grow without touching grading, persistence, or the
assembler - which is what leaves `ProfileBuilder` (#16) room to decide how a
profile advances.
"""

from __future__ import annotations

import types
from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping


@dataclass(frozen=True, slots=True)
class LearnerProfile:
    """One learner's ledger and narrative, and how far the builder has read.

    Attributes:
      learner_id: Whose profile this is. CONTEXT's "partition key for
        everything", and the key `LearnerProfileRepository` stores it under -
        not a shape-bearing internal (#30, #31).
      ledger: The exactly-recomputed figures, sealed read-only at construction
        so a caller's dict cannot rewrite a stored profile afterwards.
      narrative: The folded prose summary. `""` before anything has been folded;
        there is one representation of "nothing yet", not two.
      watermark: The last attempt `ProfileBuilder` processed, or `None` before
        the job has ever run. Bookkeeping - it never reaches a prompt.
      updated_at: When the profile was last written, or `None`. Carried and
        round-tripped; the domain does not read it.
    """

    learner_id: str
    ledger: Mapping[str, int] = field(default_factory=dict)
    narrative: str = ""
    watermark: str | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "ledger", types.MappingProxyType(dict(self.ledger))
        )
