"""The overlay's client, driven in a child Node interpreter (#13, §11).

Token rotation, verdict-to-celebration, the probe prompt, the rating prompt and
the late-pedagogy guard are true only in the client, so this is where they are
asserted — against the bytes `overlay.py` actually inlines, not against a
transcription of them.

`client.js` is written as a state machine over two injected ports, a
`transport` and a `view`, which is what makes that possible with no DOM and no
network: the tests hand it a recording stub for each and read back what it did.
Everything left in `createDomView` is binding, and is thin by construction in
the same sense the Pipe is.

One child process per case, the way `test_import_hygiene` runs its guard in a
child Python interpreter. The module skips wholesale when `node` is not on
`PATH`, as the adapter's tests skip without the `anthropic` extra: Node is a
test-time tool here and nothing else — no `package.json`, no dependency, and
nothing in the image.
"""

import json
import os
import pathlib
import shutil
import subprocess

import pytest

NODE = shutil.which("node")

if NODE is None:  # pragma: no cover - environment-dependent
    pytest.skip(
        "the client's tests run it in a child Node interpreter; install Node "
        "to exercise them. Nothing shipped depends on it.",
        allow_module_level=True,
    )

CLIENT_JS = (
    pathlib.Path(__file__).resolve().parents[1]
    / "src"
    / "socratic"
    / "ui"
    / "client.js"
)

BASE_URL = "http://localhost:8080"
SESSION = "01J000000000000000000000AA"
TOKEN = "token-one"
ROTATED = "token-two"

_PRELUDE = """
import fs from 'node:fs';

const source = fs.readFileSync(process.env.SOCRATIC_CLIENT_JS, 'utf8');
const SocraticQuiz = new Function(source + '\\nreturn SocraticQuiz;')();

const requests = [];
const calls = [];

// Every view method records its name and its arguments, so a test asserts on
// what the client *decided* rather than on how a DOM would show it.
const view = new Proxy({}, {
  get(_target, name) {
    return function () {
      calls.push({ name: name, args: Array.prototype.slice.call(arguments) });
    };
  }
});

function transportOf(responses) {
  let index = 0;
  return function (path, token, body) {
    requests.push({ path: path, token: token, body: body });
    const next = responses[Math.min(index, responses.length - 1)];
    index += 1;
    if (next && next.__reject) {
      return Promise.reject(new Error(next.__reject));
    }
    return Promise.resolve(next);
  };
}

function clientWith(responses, overrides) {
  return SocraticQuiz.createQuizClient(Object.assign({
    sessionId: '%(session)s',
    token: '%(token)s',
    transport: transportOf(responses),
    view: view
  }, overrides || {}));
}

function emit(extra) {
  console.log(JSON.stringify(Object.assign({
    requests: requests,
    calls: calls
  }, extra || {})));
}

function named(name) {
  return calls.filter(function (call) { return call.name === name; });
}
""" % {
    "session": SESSION,
    "token": TOKEN,
}


def run_js(body: str) -> dict:
    """Run one case in a child Node interpreter and return what it emitted.

    Both ends are UTF-8: Node writes it unconditionally and the parent is told
    so explicitly, because what comes back on failure is a stack trace quoting
    the client's own source (#25, #43).
    """
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", _PRELUDE + body],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "SOCRATIC_CLIENT_JS": str(CLIENT_JS)},
    )
    assert result.returncode == 0, result.stderr
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines, f"the case emitted nothing; stderr was: {result.stderr}"
    return json.loads(lines[-1])


def graded(**overrides) -> str:
    """A grading response as JSON, with the wire's full key set present.

    Spelled out rather than built by the client's defaults, because a field the
    service always sends and the client ignores is exactly the kind of drift
    these tests exist to catch.
    """
    body = {
        "verdict": "correct",
        "graded_by": "deterministic",
        "hint_rung_shown": None,
        "feedback_html": "<p>Entropy is the one that never decreases.</p>",
        "tutor_line_html": None,
        "revealed_option_id": None,
        "blank_resolved": True,
        "attempt_sealed": False,
        "probe": None,
        "capability_token": ROTATED,
    }
    body.update(overrides)
    return json.dumps(body)


def call_names(emitted: dict) -> list:
    return [call["name"] for call in emitted["calls"]]


def only(emitted: dict, name: str) -> dict:
    matching = [call for call in emitted["calls"] if call["name"] == name]
    assert len(matching) == 1, f"{name} was called {len(matching)} time(s)"
    return matching[0]


class TestTheFetchTransport:
    """`fetch` is the only answer path (D15, ADR-0015), and the capability
    token is the whole authorization story — the iframe has an opaque origin
    and carries no ambient credential at all."""

    def test_an_answer_is_one_post_carrying_the_token_in_its_header(self):
        emitted = run_js(
            """
            const seen = [];
            const fakeFetch = function (url, init) {
              seen.push({ url: url, init: init });
              return Promise.resolve({
                ok: true,
                json: function () { return Promise.resolve(%s); }
              });
            };
            const client = SocraticQuiz.createQuizClient({
              sessionId: '%s',
              token: '%s',
              transport: SocraticQuiz.createFetchTransport(fakeFetch, '%s'),
              view: view
            });
            await client.submitAnswer('b1', 'o1');
            emit({ seen: seen });
            """
            % (graded(), SESSION, TOKEN, BASE_URL)
        )

        assert len(emitted["seen"]) == 1
        request = emitted["seen"][0]
        assert request["url"] == f"{BASE_URL}/answers"
        assert request["init"]["method"] == "POST"
        assert request["init"]["headers"]["X-Socratic-Token"] == TOKEN
        assert request["init"]["headers"]["content-type"] == "application/json"
        assert json.loads(request["init"]["body"]) == {
            "quiz_session_id": SESSION,
            "blank_id": "b1",
            "submitted": "o1",
        }

    def test_a_refused_request_reaches_the_view_rather_than_being_swallowed(self):
        emitted = run_js(
            """
            const fakeFetch = function () {
              return Promise.resolve({ ok: false, status: 401, json: function () {
                return Promise.resolve({ detail: 'unauthorized' });
              } });
            };
            const client = SocraticQuiz.createQuizClient({
              sessionId: '%s',
              token: '%s',
              transport: SocraticQuiz.createFetchTransport(fakeFetch, '%s'),
              view: view
            });
            await client.submitAnswer('b1', 'o1');
            emit({ token: client.token() });
            """
            % (SESSION, TOKEN, BASE_URL)
        )

        assert "failed" in call_names(emitted)
        assert "celebrate" not in call_names(emitted)
        assert emitted["token"] == TOKEN


class TestTokenRotation:
    """Each grading response returns a fresh token the iframe swaps in, and
    rotation is supersession: the old one stops being accepted (#18, D15)."""

    def test_the_rotated_token_replaces_the_previous_one(self):
        emitted = run_js(
            """
            const client = clientWith([%s]);
            await client.submitAnswer('b1', 'o1');
            emit({ token: client.token() });
            """
            % graded()
        )

        assert emitted["token"] == ROTATED

    def test_the_next_request_carries_the_new_token_and_never_the_old(self):
        emitted = run_js(
            """
            const client = clientWith([%s, %s]);
            await client.submitAnswer('b1', 'o1');
            await client.submitAnswer('b2', 'o3');
            emit({});
            """
            % (graded(), graded(capability_token="token-three"))
        )

        assert [request["token"] for request in emitted["requests"]] == [
            TOKEN,
            ROTATED,
        ]

    def test_a_rating_carries_no_token_and_rotates_nothing(self):
        """`/ratings` is not a grading response, so it returns none — and a
        client that swapped in the missing field would lock the learner out of
        their own quiz with an `undefined` credential."""
        emitted = run_js(
            """
            const client = clientWith([{ ok: true }]);
            await client.submitRating(4);
            emit({ token: client.token() });
            """
        )

        assert emitted["token"] == TOKEN
        assert emitted["requests"][0]["token"] == TOKEN

    def test_a_failed_request_does_not_rotate(self):
        emitted = run_js(
            """
            const client = clientWith([{ __reject: 'network down' }]);
            await client.submitAnswer('b1', 'o1');
            emit({ token: client.token() });
            """
        )

        assert emitted["token"] == TOKEN
        assert "failed" in call_names(emitted)


class TestTheVerdict:
    """The verdict starts the celebration; the Advanced tutor line is already
    in that response; nothing streams (D13, ADR-0013)."""

    def test_a_correct_verdict_starts_the_celebration(self):
        emitted = run_js(
            """
            const client = clientWith([%s]);
            await client.submitAnswer('b1', 'o1');
            emit({});
            """
            % graded()
        )

        celebrate = only(emitted, "celebrate")
        assert celebrate["args"][0]["blankId"] == "b1"
        assert "never decreases" in celebrate["args"][0]["feedbackHtml"]
        assert len(emitted["requests"]) == 1

    def test_the_tutor_line_arrives_in_the_same_response(self):
        """One response, three things. A second request for the commentary is
        the withdrawn parallel call (ADR-0013), and there is no stream to catch
        up afterwards."""
        emitted = run_js(
            """
            const client = clientWith([%s]);
            await client.submitAnswer('b1', 'a rise in disorder');
            emit({});
            """
            % graded(
                graded_by="model_graded",
                tutor_line_html="<p>You reached for the macro picture.</p>",
            )
        )

        celebrate = only(emitted, "celebrate")
        assert (
            "You reached for the macro picture."
            in celebrate["args"][0]["tutorLineHtml"]
        )
        assert len(emitted["requests"]) == 1

    def test_novice_has_no_tutor_line_and_the_celebration_still_runs(self):
        emitted = run_js(
            """
            const client = clientWith([%s]);
            await client.submitAnswer('b1', 'o1');
            emit({});
            """
            % graded(tutor_line_html=None)
        )

        assert only(emitted, "celebrate")["args"][0]["tutorLineHtml"] is None

    def test_a_wrong_answer_shows_the_rung_it_landed_on(self):
        emitted = run_js(
            """
            const client = clientWith([%s]);
            await client.submitAnswer('b1', 'o2');
            emit({});
            """
            % graded(
                verdict="incorrect",
                hint_rung_shown=2,
                feedback_html="<p>Think about what cannot decrease.</p>",
                blank_resolved=False,
            )
        )

        hint = only(emitted, "showHint")["args"][0]
        assert hint["rung"] == 2
        assert "cannot decrease" in hint["feedbackHtml"]
        assert "celebrate" not in call_names(emitted)

    def test_the_rung_three_reveal_reaches_the_view(self):
        emitted = run_js(
            """
            const client = clientWith([%s]);
            await client.submitAnswer('b1', 'o2');
            emit({});
            """
            % graded(
                verdict="incorrect",
                hint_rung_shown=3,
                feedback_html="<p>It is entropy.</p>",
                revealed_option_id="o1",
                blank_resolved=True,
            )
        )

        assert only(emitted, "showHint")["args"][0]["revealedOptionId"] == "o1"
        assert "resolveBlank" in call_names(emitted)

    def test_a_resolved_blank_is_marked_resolved(self):
        emitted = run_js(
            """
            const client = clientWith([%s]);
            await client.submitAnswer('b1', 'o1');
            emit({});
            """
            % graded()
        )

        assert only(emitted, "resolveBlank")["args"][0]["blankId"] == "b1"

    def test_a_resolved_blank_is_told_what_to_put_in_the_gap(self):
        """Master acceptance 31 is "no silent blanks", and a finished quiz whose
        every blank is an empty gap is exactly that. What the learner got right
        is what fills it — the service sends no resolved text on a correct
        answer, and a client that waited for one would leave a hole."""
        emitted = run_js(
            """
            const client = clientWith([%s]);
            await client.submitAnswer('b1', 'o1');
            emit({});
            """
            % graded()
        )

        assert only(emitted, "resolveBlank")["args"][0]["answer"] == "o1"

    def test_a_rung_three_novice_close_puts_nothing_in_the_gap(self):
        """Not the revealed option either (#112). The reveal names the answer
        in the note, and a blank the learner did not earn reads the same way in
        both modes: closed, asserting nothing."""
        emitted = run_js(
            """
            const client = clientWith([%s]);
            await client.submitAnswer('b1', 'o2');
            emit({});
            """
            % graded(
                verdict="incorrect",
                hint_rung_shown=3,
                revealed_option_id="o1",
                blank_resolved=True,
            )
        )

        assert only(emitted, "resolveBlank")["args"][0]["answer"] is None

    def test_a_rung_three_advanced_close_never_writes_the_wrong_answer_in(self):
        """The bug #112 reports. An Advanced blank carries no option id at all —
        its rubric is the answer key and must not be returned (ADR-0003) — so
        the gap used to fall through to what the learner submitted, which on a
        rung-three close is by definition wrong."""
        emitted = run_js(
            """
            const client = clientWith([%s]);
            await client.submitAnswer('b1', 'enthalpy');
            emit({});
            """
            % graded(
                verdict="incorrect",
                hint_rung_shown=3,
                feedback_html="<p>It is entropy.</p>",
                revealed_option_id=None,
                blank_resolved=True,
            )
        )

        assert only(emitted, "resolveBlank")["args"][0]["answer"] is None
        assert "enthalpy" not in json.dumps(only(emitted, "resolveBlank"))

    def test_a_rung_three_close_still_closes_the_blank_and_moves_on(self):
        """Asserting nothing is not the same as staying open. Rung three closes
        the blank (ADR-0009) and the learner goes to the next one."""
        emitted = run_js(
            """
            const client = clientWith([%s], { blankIds: ['b1', 'b2'] });
            await client.submitAnswer('b1', 'enthalpy');
            emit({});
            """
            % graded(
                verdict="incorrect",
                hint_rung_shown=3,
                revealed_option_id=None,
                blank_resolved=True,
            )
        )

        assert only(emitted, "resolveBlank")["args"][0]["blankId"] == "b1"
        assert only(emitted, "activateBlank")["args"][0] == "b2"

    def test_a_closed_gap_is_told_what_to_say(self):
        """Master acceptance 31 is "no silent blanks", and a gap that asserts
        nothing must still not be empty. The text is the state machine's to
        supply for exactly that reason: the DOM half has no tests, so a marker
        chosen there could be emptied and the suite would stay green (#128
        review)."""
        emitted = run_js(
            """
            const client = clientWith([%s]);
            await client.submitAnswer('b1', 'enthalpy');
            emit({});
            """
            % graded(
                verdict="incorrect",
                hint_rung_shown=3,
                revealed_option_id=None,
                blank_resolved=True,
            )
        )

        closed_text = only(emitted, "resolveBlank")["args"][0]["closedText"]
        assert isinstance(closed_text, str)
        assert closed_text.strip()

    def test_a_gap_the_learner_earned_is_told_nothing_to_say(self):
        """The marker is for a close the learner did not earn. A correct answer
        fills the gap with the answer itself and needs no stand-in."""
        emitted = run_js(
            """
            const client = clientWith([%s]);
            await client.submitAnswer('b1', 'o1');
            emit({});
            """
            % graded()
        )

        assert only(emitted, "resolveBlank")["args"][0]["closedText"] is None

    def test_a_wrong_answer_that_leaves_the_blank_open_resolves_nothing(self):
        """Rungs one and two say nothing about the gap; the blank is still the
        learner's to answer."""
        emitted = run_js(
            """
            const client = clientWith([%s]);
            await client.submitAnswer('b1', 'o2');
            emit({});
            """
            % graded(
                verdict="incorrect",
                hint_rung_shown=2,
                blank_resolved=False,
            )
        )

        assert "resolveBlank" not in call_names(emitted)

    def test_the_grade_event_says_when_a_blank_closed_on_a_reveal(self):
        """What the note keys off. It is derived from the verdict rather than
        from the rung, because what matters is that the blank closed without
        the learner getting it right — true in both modes, and the client
        cannot see the mode."""
        emitted = run_js(
            """
            const client = clientWith([%s]);
            await client.submitAnswer('b1', 'o2');
            emit({});
            """
            % graded(
                verdict="incorrect",
                hint_rung_shown=3,
                revealed_option_id="o1",
                blank_resolved=True,
            )
        )

        assert only(emitted, "showHint")["args"][0]["reveal"] is True

    def test_a_correct_answer_is_not_a_reveal(self):
        emitted = run_js(
            """
            const client = clientWith([%s]);
            await client.submitAnswer('b1', 'o1');
            emit({});
            """
            % graded()
        )

        assert only(emitted, "celebrate")["args"][0]["reveal"] is False

    def test_a_wrong_answer_on_an_open_blank_is_not_a_reveal(self):
        emitted = run_js(
            """
            const client = clientWith([%s]);
            await client.submitAnswer('b1', 'o2');
            emit({});
            """
            % graded(
                verdict="incorrect",
                hint_rung_shown=1,
                blank_resolved=False,
            )
        )

        assert only(emitted, "showHint")["args"][0]["reveal"] is False


class TestTheRevealNote:
    """The note is the whole disclosure on a rung-three close, because the gap
    no longer carries any of it (#112). Composing it is a decision, so it is a
    pure function on the module rather than a line inside `createDomView` —
    which has no tests, being binding only."""

    def test_it_names_the_option_where_there_is_one(self):
        emitted = run_js(
            """
            emit({ note: SocraticQuiz.revealNote('entropy', true) });
            """
        )

        assert emitted["note"].startswith("The answer: entropy.")

    def test_the_option_clause_is_a_finished_sentence(self):
        """`payloads.label_html` renders a bare phrase with no terminator
        (#98), so the note has to punctuate it or the two sentences run
        together (#128 review)."""
        note = run_js("emit({ note: SocraticQuiz.revealNote('entropy', true) });")[
            "note"
        ]

        assert "entropy You can" not in note
        assert "entropy. You can" in note

    def test_a_prose_reveal_leaves_the_answer_to_the_feedback_above_it(self):
        """No option id means the reveal is prose, and it arrives in the
        feedback block directly above the note. The note does not repeat it —
        it has no way to."""
        emitted = run_js(
            """
            emit({ note: SocraticQuiz.revealNote(null, true) });
            """
        )

        assert "The answer:" not in emitted["note"]
        assert emitted["note"].strip()

    def test_a_reveal_that_names_no_answer_anywhere_says_so(self):
        """A hint dropped for reproducing the key leaves `feedback` null
        (`_safe_hint`), so on a prose reveal nothing on screen names the
        answer. The note says that rather than sending the learner to look for
        something that is not there (#128 review)."""
        emitted = run_js(
            """
            emit({
              silent: SocraticQuiz.revealNote(null, false),
              spoken: SocraticQuiz.revealNote(null, true)
            });
            """
        )

        assert emitted["silent"].strip()
        assert emitted["silent"] != emitted["spoken"]


class TestTheLatePedagogyWindow:
    """A learner can answer before the pedagogy payload lands (master spec
    acceptance 5, D11). Missing pedagogy costs the hint text, not the verdict —
    `_hint_for_rung` already returns `None` rather than raising, and the client
    is the other half of that."""

    def test_a_verdict_with_no_feedback_is_not_an_error(self):
        emitted = run_js(
            """
            const client = clientWith([%s]);
            const response = await client.submitAnswer('b1', 'o1');
            emit({ verdict: response.verdict });
            """
            % graded(feedback_html=None)
        )

        assert emitted["verdict"] == "correct"
        assert only(emitted, "celebrate")["args"][0]["feedbackHtml"] is None
        assert "failed" not in call_names(emitted)

    def test_the_view_is_told_the_pedagogy_is_pending(self):
        """Rather than being handed a null to render. The learner sees that the
        tutor is still writing, which is true, instead of an empty panel."""
        emitted = run_js(
            """
            const client = clientWith([%s]);
            await client.submitAnswer('b1', 'o2');
            emit({});
            """
            % graded(
                verdict="incorrect",
                hint_rung_shown=1,
                feedback_html=None,
                blank_resolved=False,
            )
        )

        hint = only(emitted, "showHint")["args"][0]
        assert hint["pedagogyPending"] is True
        assert hint["rung"] == 1

    def test_pedagogy_that_did_land_is_not_reported_pending(self):
        emitted = run_js(
            """
            const client = clientWith([%s]);
            await client.submitAnswer('b1', 'o1');
            emit({});
            """
            % graded()
        )

        assert only(emitted, "celebrate")["args"][0]["pedagogyPending"] is False

    def test_no_view_argument_is_ever_the_string_null_or_undefined(self):
        """The failure this guards against is cosmetic and unmistakable: a
        panel reading "undefined" where a hint should be."""
        emitted = run_js(
            """
            const client = clientWith([%s]);
            await client.submitAnswer('b1', 'o2');
            emit({});
            """
            % graded(
                verdict="incorrect",
                hint_rung_shown=1,
                feedback_html=None,
                tutor_line_html=None,
                blank_resolved=False,
            )
        )

        blob = json.dumps(emitted["calls"])
        assert '"null"' not in blob
        assert '"undefined"' not in blob


PROBE = {"blank_id": "b1", "question_html": "<p>How did you arrive at that?</p>"}
"""One probe as it crosses the wire — `payloads._probe_body`'s shape."""


PROBE_ANSWERED = {
    "verdict": "correct",
    "correction_html": None,
    "blank_reopened": False,
    "blank_resolved": True,
    "revealed_option_id": None,
    "attempt_sealed": True,
    "capability_token": ROTATED,
}


class TestWalkingTheBlanks:
    """One blank at a time, in the order the segment walk declared them.

    The order is the quiz's, not the learner's: the final blank is the last one
    the quiz *declares* (acceptance 19, `_cadence_says_probe`), so the client
    walks the same list the server probed against. Holding it in the state
    machine rather than in the DOM is what makes "the learner can always reach
    the next blank" assertable — the bug it guards against is a resolved blank
    that hides its own control and reveals nothing.
    """

    def test_the_first_blank_is_the_one_the_learner_starts_on(self):
        emitted = run_js(
            """
            const client = clientWith([%s], { blankIds: ['b1', 'b2', 'b3'] });
            client.start();
            emit({});
            """
            % graded()
        )

        assert only(emitted, "activateBlank")["args"][0] == "b1"

    def test_resolving_a_blank_activates_the_next_one(self):
        emitted = run_js(
            """
            const client = clientWith([%s], { blankIds: ['b1', 'b2'] });
            await client.submitAnswer('b1', 'o1');
            emit({});
            """
            % graded()
        )

        assert only(emitted, "activateBlank")["args"][0] == "b2"

    def test_a_wrong_answer_leaves_the_learner_on_the_same_blank(self):
        emitted = run_js(
            """
            const client = clientWith([%s], { blankIds: ['b1', 'b2'] });
            await client.submitAnswer('b1', 'o2');
            emit({});
            """
            % graded(verdict="incorrect", hint_rung_shown=1, blank_resolved=False)
        )

        assert "activateBlank" not in call_names(emitted)

    def test_the_last_blank_resolving_activates_nothing_and_reports_the_end(self):
        emitted = run_js(
            """
            const client = clientWith([%s], { blankIds: ['b1'] });
            await client.submitAnswer('b1', 'o1');
            emit({});
            """
            % graded(attempt_sealed=True)
        )

        assert "activateBlank" not in call_names(emitted)
        assert "allBlanksResolved" in call_names(emitted)
        assert "showRating" in call_names(emitted)

    def test_a_probe_that_reopens_a_blank_makes_it_active_again(self):
        """`resolved` is not a terminal state: a failed probe in Advanced
        re-opens the blank and the ladder resumes where it left off
        (ADR-0009)."""
        emitted = run_js(
            """
            const client = clientWith([%s, %s], { blankIds: ['b1', 'b2'] });
            await client.submitAnswer('b1', 'o1');
            await client.answerProbe('b1', 'I guessed');
            emit({});
            """
            % (
                graded(probe=PROBE),
                json.dumps(
                    {
                        "verdict": "incorrect",
                        "correction_html": "<p>Heat is not disorder.</p>",
                        "blank_reopened": True,
                        "blank_resolved": False,
                        "revealed_option_id": None,
                        "attempt_sealed": False,
                        "capability_token": "token-three",
                    }
                ),
            )
        )

        activated = [call["args"][0] for call in emitted["calls"]
                     if call["name"] == "activateBlank"]
        assert activated == ["b2", "b1"]

    def test_a_client_told_no_blank_order_still_grades_answers(self):
        """The order is an optimisation for the walk, not a precondition for
        submitting — a quiz whose document the client could not read must still
        return a verdict rather than throw."""
        emitted = run_js(
            """
            const client = clientWith([%s]);
            await client.submitAnswer('b1', 'o1');
            emit({});
            """
            % graded()
        )

        assert "celebrate" in call_names(emitted)
        assert "failed" not in call_names(emitted)


class TestTheProbe:
    """A probe prompt appears when the response carries one, and is
    dismissible. The cadence is decided server-side and arrives as the `probe`
    field; the client shows what it is given (D9, ADR-0009)."""

    def test_a_response_carrying_a_probe_shows_the_prompt(self):
        emitted = run_js(
            """
            const client = clientWith([%s]);
            await client.submitAnswer('b1', 'o1');
            emit({});
            """
            % graded(probe=PROBE)
        )

        probe = only(emitted, "showProbe")["args"][0]
        assert probe["blankId"] == "b1"
        assert "How did you arrive" in probe["questionHtml"]

    def test_a_response_with_no_probe_shows_nothing(self):
        emitted = run_js(
            """
            const client = clientWith([%s]);
            await client.submitAnswer('b1', 'o1');
            emit({});
            """
            % graded()
        )

        assert "showProbe" not in call_names(emitted)

    def test_the_celebration_starts_before_the_prompt(self):
        """The verdict is what the learner is waiting for; the probe is the
        tutor's follow-up, not the answer's result."""
        emitted = run_js(
            """
            const client = clientWith([%s]);
            await client.submitAnswer('b1', 'o1');
            emit({});
            """
            % graded(probe=PROBE)
        )

        names = call_names(emitted)
        assert names.index("celebrate") < names.index("showProbe")

    def test_answering_a_probe_posts_the_self_explanation(self):
        emitted = run_js(
            """
            const client = clientWith([%s]);
            await client.answerProbe('b1', 'because disorder rises');
            emit({});
            """
            % json.dumps(PROBE_ANSWERED)
        )

        request = emitted["requests"][0]
        assert request["path"] == "/probes"
        assert request["body"] == {
            "quiz_session_id": SESSION,
            "blank_id": "b1",
            "self_explanation": "because disorder rises",
        }
        assert "hideProbe" in call_names(emitted)

    def test_a_failed_probe_that_reopens_the_blank_says_so(self):
        emitted = run_js(
            """
            const client = clientWith([%s]);
            await client.answerProbe('b1', 'I guessed');
            emit({});
            """
            % json.dumps(
                {
                    "verdict": "incorrect",
                    "correction_html": "<p>Heat is not disorder.</p>",
                    "blank_reopened": True,
                    "blank_resolved": False,
                    "revealed_option_id": None,
                    "attempt_sealed": False,
                    "capability_token": ROTATED,
                }
            )
        )

        graded_call = only(emitted, "probeGraded")["args"][0]
        assert graded_call["blankReopened"] is True
        assert "Heat is not disorder." in graded_call["correctionHtml"]

    def test_dismissing_a_probe_sends_a_null_self_explanation(self):
        """A dismissal is a value on the same endpoint, not a fifth route
        (`quiz-service.md`), and it persists as a probe with no
        self-explanation (acceptance 18)."""
        emitted = run_js(
            """
            const client = clientWith([{ dismissed: true, attempt_sealed: false,
                                         capability_token: '%s' }]);
            await client.dismissProbe('b1');
            emit({});
            """
            % ROTATED
        )

        assert len(emitted["requests"]) == 1
        assert emitted["requests"][0]["path"] == "/probes"
        assert emitted["requests"][0]["body"]["self_explanation"] is None
        assert "hideProbe" in call_names(emitted)


class TestTheRatingPrompt:
    """Optional, dismissible, and never entering the conversation (§11,
    CONTEXT: RatingRecord, acceptance 24)."""

    def test_sealing_shows_the_prompt(self):
        emitted = run_js(
            """
            const client = clientWith([%s]);
            await client.submitAnswer('b1', 'o1');
            emit({});
            """
            % graded(attempt_sealed=True)
        )

        assert "showRating" in call_names(emitted)

    def test_an_unsealed_attempt_shows_nothing(self):
        emitted = run_js(
            """
            const client = clientWith([%s]);
            await client.submitAnswer('b1', 'o1');
            emit({});
            """
            % graded()
        )

        assert "showRating" not in call_names(emitted)

    def test_sealing_through_a_probe_also_shows_the_prompt(self):
        """A blank can seal by way of a probe, because `resolved` is not a
        terminal state (CONTEXT: Sealed)."""
        emitted = run_js(
            """
            const client = clientWith([%s]);
            await client.answerProbe('b1', 'because disorder rises');
            emit({});
            """
            % json.dumps(PROBE_ANSWERED)
        )

        assert "showRating" in call_names(emitted)

    def test_a_score_is_one_request_to_the_ratings_endpoint(self):
        emitted = run_js(
            """
            const client = clientWith([{ ok: true }]);
            await client.submitRating(5);
            emit({});
            """
        )

        assert len(emitted["requests"]) == 1
        assert emitted["requests"][0]["path"] == "/ratings"
        assert emitted["requests"][0]["body"] == {
            "quiz_session_id": SESSION,
            "score": 5,
        }
        assert "hideRating" in call_names(emitted)

    def test_dismissing_the_prompt_sends_nothing(self):
        """Optional means optional: a learner who waves it away leaves no
        `RatingRecord`, and the attempt stays sealed and unwritten."""
        emitted = run_js(
            """
            const client = clientWith([{ ok: true }]);
            client.dismissRating();
            emit({});
            """
        )

        assert emitted["requests"] == []
        assert "hideRating" in call_names(emitted)


class TestNothingStreamsAndNothingEntersTheConversation:
    """Two structural claims about the shipped bytes.

    The streaming subsystem left the prototype with the parallel tutor call
    (D13); and the only channel by which anything in a sandboxed iframe could
    reach the chat is Open WebUI's `postMessage` prompt-submission bridge,
    which creates a new chat turn and reassigns `srcdoc` (ADR-0015). A rating
    that never enters the conversation is a rating that never touches it.
    """

    SOURCE = CLIENT_JS.read_text(encoding="utf-8")

    @pytest.mark.parametrize(
        "forbidden",
        ["postMessage", "EventSource", "WebSocket", "getReader", "response.body"],
    )
    def test_the_client_never_reaches_for_it(self, forbidden):
        assert forbidden not in self.SOURCE

    def test_the_client_never_branches_on_a_difficulty_mode(self):
        """The Python scan in `test_registry.py` walks `*.py` only, so the one
        file where the view could branch on mode is the one file it cannot
        see. The control comes from `render_hint`, server-side."""
        lowered = self.SOURCE.lower()

        assert "novice" not in lowered
        assert "advanced" not in lowered
