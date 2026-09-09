# A quiz session id is a ULID, and the Quiz says so

## Goal

[ADR-0007](../adr/0007-mint-our-own-quiz-session-id.md) decides the primary key
of a quiz session is a **minted ULID** (CONTEXT: QuizSessionId), and
`ids.new_quiz_session_id` mints one. Nothing checks it. `Quiz.__post_init__`
(`src/socratic/domain/types.py:113`) validates duplicate blank ids and the
explanation/blank correspondence and nothing else, so a `Quiz` built with `""`,
`"session-1"` or a borrowed Open WebUI `chat_id` is admitted.

The asymmetry is visible inside one record. `QuizAttempt.__post_init__`
(`src/socratic/domain/records.py:237`) parses `attempt_id` as a ULID outright,
and since #58 its `session_id` is checked only *relative* to the quiz's — so an
unparseable session id passes both fields rather than neither.

Two modules argue from the shape rather than merely from uniqueness:
`ids.py`'s module docstring and `tokens.py:15` both reason from the ULID's
timestamp component, `tokens.py` to explain that a `QuizSessionId` is
deliberately **not** a credential *because* it is timestamp-prefixed and
therefore guessable. That is a claim about a ULID, not about an arbitrary
string, and today nothing makes it true. Closes #64.

## Seams

No new seam. The check attaches to the constructor that already validates the
record:

| Seam | Kind | Why it must exist |
|---|---|---|
| `Quiz.__post_init__` | existing | The one place every `Quiz` passes through, however it is built — minted, parsed from an authoring payload, or reconstructed by a repository. Already the home of the record's other invariants. |
| `Ulid.parse(value) -> Ulid` | existing | The parser `QuizAttempt.__post_init__` already uses for `attempt_id`. Raises `ValueError` on a wrong length or a non-Crockford character; nothing new is needed to express "is a ULID". |
| `QuizAttempt.__post_init__` | existing, unchanged | Its `session_id == quiz.quiz_session_id` equality, added by #58, becomes a transitive ULID guarantee once the quiz's own id is checked. No second parse is added here. |

## Decisions

- **The check belongs on `Quiz`, not on a new value type.** ADR-0007 makes the
  session id the quiz's primary key; `Quiz` is the record that holds it, and
  `QuizAttempt` already sets the precedent of parsing its own key in
  `__post_init__`. The issue's alternative — promoting `QuizSessionId` from a
  `str` alias to a real value type — would move the guarantee to the type and
  reach `QuizAttempt.session_id` directly, but it touches every module that
  passes a session id around, including the service payloads, the token minter
  and the overlay. That is a larger and less reversible change than the defect
  warrants, and nothing in it is foreclosed by adding the guard now.
- **`QuizAttempt.session_id` gets no parse of its own.** After #58 an attempt
  and its quiz must name one session; with the quiz's id parsed, the attempt's
  is a ULID by equality. A second `Ulid.parse` would be a second statement of
  the same invariant, and #82 is the repo's own precedent for not keeping two
  guards for one rule.
- **The guard runs first, before the blank checks.** `QuizAttempt` parses its
  key before validating its contents; a record whose identity is malformed
  should say so before it reports on what it contains.
- **The message mirrors `records.py`'s.** "a quiz session is keyed by a ULID:
  `<value>` (`<reason>`)", so the two sibling failures read alike and both
  `pytest.raises(ValueError, match="ULID")` guards match on the same word.
- **Only one fixture in the suite is affected.** A sweep of every `Quiz(`
  construction in `src`, `tests` and `pipe` (14 sites) finds one file passing a
  non-ULID: `tests/test_output_schemas.py` uses the placeholder
  `"quiz-session"` at lines 145 and 159. Every other fixture already mints —
  `new_quiz_session_id()`, `str(Ulid.mint())`, or a literal ULID. The fixtures
  are fixed to mint, never the check weakened.
- **No production path passes a non-ULID.** The only `Quiz` construction in
  `src` is `authoring._parse_quiz` (`authoring.py:448`), whose `quiz_session_id`
  argument comes from `ids.new_quiz_session_id` at `authoring.py:205` and from
  nowhere else. The guard is a programming-error trap, not a new refusal on the
  learner's path.

## Approach

Red → green at the one seam, then the fixture sweep:

1. Red: `tests/test_types.py::TestQuiz` gains
   `test_the_quiz_session_id_is_a_ulid`,
   `test_a_non_ulid_quiz_session_id_is_rejected` and
   `test_an_empty_quiz_session_id_is_rejected`, mirroring the `attempt_id`
   pair at `tests/test_records.py:224`. Watch the last two fail by *admitting*
   the bad id — the symptom #64 reports.
2. Green: `Quiz.__post_init__` opens with a `Ulid.parse` guard; `types.py`
   imports `Ulid` alongside `QuizSessionId` from `ids`.
3. Fix `tests/test_output_schemas.py`'s two `"quiz-session"` placeholders to
   minted ids.
4. Full suite.

## Out of scope

- **Promoting `QuizSessionId` to a value type.** Weighed above and deferred;
  the alias stays a `str`.
- **`AttemptId`, `LearnerId`, `blank_id`, `option_id`.** `attempt_id` is already
  parsed; the others are not ULIDs by any decision and are untouched.
- **`_parse_quiz`'s `except ValueError -> AuthoringParseError` wrapper.** A
  non-ULID session id reaching it would now be reported as a malformed
  authoring payload, which misattributes a caller bug to the model. It is
  unreachable — the only caller mints — and narrowing the wrapper is a separate
  change.
- **The host annotations.** Open WebUI's `chat_id` and `session_id` are not
  keys (CONTEXT: Host annotations) and get no shape check.
- **Service-level validation.** `payloads.py:69` keeps `min_length=1` on the
  request field; the domain refusing a bad id is the guarantee, and turning a
  domain `ValueError` into a 4xx is not this change.

## Acceptance

1. `Quiz(quiz_session_id="not-a-ulid", ...)` raises `ValueError` naming ULID —
   the exact reproduction in #64 is refused.
2. `Quiz(quiz_session_id="", ...)` is refused for the same reason.
3. A `Quiz` built by `new_quiz_session_id()` is admitted, and
   `Ulid.parse(quiz.quiz_session_id).timestamp` is a real mint time.
4. `QuizAttempt` with a non-ULID `session_id` is still refused — now on the
   quiz's id rather than only on the mismatch.
5. No fixture in the suite builds a `Quiz` with a non-ULID session id.
6. Full suite green, three tests more than the baseline (1024 passed, 2
   skipped → 1027 passed, 2 skipped), no skips gained.
