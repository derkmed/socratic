/* The overlay's client: one classic script assigning one global.
 *
 * Classic and single-global on purpose. The browser gets `window.SocraticQuiz`
 * from an inline `<script>` in a `srcdoc` document with an opaque origin, where
 * a module's import machinery has nothing to resolve against; and a child Node
 * interpreter can evaluate these same bytes and pull the factory out, which is
 * how `tests/test_ui_client.py` drives the state machine with no DOM and no
 * network.
 *
 * Three shapes, and the split between them is what makes the ticket testable:
 *
 *   createFetchTransport  the only place `fetch` is named
 *   createQuizClient      every decision, over an injected transport and view
 *   createDomView / mount binding, and nothing else
 *
 * Nothing runs at load time. `mount` is called by the document.
 *
 * Nothing streams (D13, ADR-0013): one request per interaction, one response
 * carrying the verdict, the probe question and the tutor line together. And
 * nothing here reaches the chat — no prompt-submission bridge, which is the only
 * channel a sandboxed iframe has to it (ADR-0015). The rating goes to the
 * service and stays out of the conversation.
 */
var SocraticQuiz = (function () {
  "use strict";

  var TOKEN_HEADER = "X-Socratic-Token";

  var PATHS = {
    answers: "/answers",
    probes: "/probes",
    ratings: "/ratings"
  };

  var CORRECT = "correct";

  /* What a reveal puts on screen. The gap's marker is handed to the view on
   * the event, and the note is composed by a function the tests can call:
   * "the gap says something" and "the note shows on every reveal" are the two
   * decisions #112's remedy consists of, and `createDomView` has no tests to
   * hold either of them (#128 review). */
  var SEE_THE_NOTE = "— see the note";
  var CARRY_ON = "You can now continue with these answers in mind.";
  var NO_ANSWER_NAMED = "The tutor did not name the answer for this one.";

  /* The note's text on a rung-three close.
   *
   * Args:
   *   optionHtml: The revealed option's label, already sanitised in the
   *     service, or null where the reveal carries no option id.
   *   hasFeedback: Whether the verdict panel's feedback block has anything in
   *     it. Where the reveal is prose, that block is where it landed.
   *
   * Returns:
   *   Inline HTML for the note. The option clause is punctuated here because
   *   `payloads.label_html` renders a bare phrase with no terminator (#98).
   *   With no option id and no feedback nothing on screen names the answer —
   *   a hint dropped for reproducing the key leaves `feedback` null — so the
   *   note says that, rather than telling the learner to carry on with an
   *   answer they were never given.
   */
  function revealNote(optionHtml, hasFeedback) {
    if (optionHtml) {
      return "The answer: " + optionHtml + ". " + CARRY_ON;
    }
    return hasFeedback ? CARRY_ON : NO_ANSWER_NAMED;
  }

  /* --- The transport ------------------------------------------------------ */

  /* `fetch`, with the capability token in a header.
   *
   * A header rather than anything ambient: the iframe is sandboxed without
   * `allow-same-origin`, so it has an opaque origin and carries no cookie and
   * no host token at all (D15). The URL is absolute because a `srcdoc`
   * document inherits its parent's base URL — a relative path would leave for
   * Open WebUI's origin instead of the service's.
   */
  function createFetchTransport(fetchImpl, baseUrl) {
    var root = String(baseUrl).replace(/\/+$/, "");

    return function (path, token, payload) {
      var headers = { "content-type": "application/json" };
      headers[TOKEN_HEADER] = token;

      return fetchImpl(root + path, {
        method: "POST",
        headers: headers,
        body: JSON.stringify(payload)
      }).then(function (reply) {
        if (!reply.ok) {
          throw new Error("the quiz service refused the request: " + reply.status);
        }
        return reply.json();
      });
    };
  }

  /* --- The state machine -------------------------------------------------- */

  /* Every decision the overlay makes, over two injected ports.
   *
   * It holds exactly one piece of mutable state: the current capability token.
   * Everything else about the attempt is the service's — the ladder rung, the
   * cadence, whether the attempt has sealed — and arrives on each response.
   * That is ADR-0001's client-authoritative state read the right way round: the
   * client owns the *interaction*, the service owns the *judgement*.
   */
  function createQuizClient(options) {
    var sessionId = options.sessionId;
    var token = options.token;
    var transport = options.transport;
    var view = options.view;

    /* The blanks in the order the quiz declared them — the order the segment
     * walk drew them, and the order the server's cadence called the last one
     * final (acceptance 19). Held here rather than read out of the DOM each
     * time so that "the learner can always reach the next blank" is a decision
     * this file makes and the tests can see. */
    var blankIds = (options.blankIds || []).slice();
    var resolved = {};

    function send(path, payload) {
      var body = { quiz_session_id: sessionId };
      for (var key in payload) {
        if (Object.prototype.hasOwnProperty.call(payload, key)) {
          body[key] = payload[key];
        }
      }

      return transport(path, token, body).then(function (reply) {
        /* Rotation is supersession, not addition: the previous token stops
         * being accepted, which is what keeps the exposure window at minutes
         * rather than the life of the quiz (#18, D15). A response with no
         * token — the rating — leaves the current one alone; swapping in a
         * missing field would lock the learner out with an `undefined`
         * credential. */
        if (reply && typeof reply.capability_token === "string") {
          token = reply.capability_token;
        }
        return reply;
      });
    }

    /* A rejected request is reported, not swallowed and not retried. The token
     * is untouched, so the learner can try the same answer again. */
    function guarded(promise) {
      return promise["catch"](function (error) {
        view.failed(error);
        return null;
      });
    }

    function orNull(value) {
      return value === undefined || value === "" ? null : value;
    }

    /* Move to the first blank still open, or say there is none left. Called on
     * every resolution rather than tracking a cursor, because a probe can
     * re-open a blank behind the one the learner is on: `resolved` is not a
     * terminal state (CONTEXT: Sealed), so a cursor would walk past a blank
     * that came back. */
    function advance() {
      for (var index = 0; index < blankIds.length; index += 1) {
        if (!resolved[blankIds[index]]) {
          view.activateBlank(blankIds[index]);
          return;
        }
      }
      if (blankIds.length) {
        view.allBlanksResolved();
      }
    }

    function start() {
      advance();
    }

    function submitAnswer(blankId, submitted) {
      return guarded(
        send(PATHS.answers, {
          blank_id: blankId,
          submitted: submitted
        }).then(function (reply) {
          applyGrade(blankId, submitted, reply);
          return reply;
        })
      );
    }

    function applyGrade(blankId, submitted, reply) {
      var feedbackHtml = orNull(reply.feedback_html);
      var revealedOptionId = orNull(reply.revealed_option_id);
      /* A rung-three close: the blank closed and the learner did not get it
       * right, so this response carries the reveal (CONTEXT: Reveal) and the
       * note is where the answer is named. Derived from the verdict rather
       * than from `hint_rung_shown === 3` because what matters is that the
       * blank closed unearned. True in both modes — the client cannot see the
       * mode and does not need to. */
      var reveal = reply.verdict !== CORRECT && Boolean(reply.blank_resolved);

      var event = {
        blankId: blankId,
        feedbackHtml: feedbackHtml,
        tutorLineHtml: orNull(reply.tutor_line_html),
        /* The window the split authoring call opens: the quiz is playable on
         * the skeleton alone, so a learner can answer before the pedagogy
         * payload lands (master spec acceptance 5, D11). Missing pedagogy costs
         * the text, not the verdict — the view is told it is pending rather
         * than handed a null to render.
         *
         * A reveal carrying no option id is the one place a missing text is
         * absent rather than late: that reveal is prose authored on this very
         * response (ADR-0016), so nothing is still being written, and saying
         * otherwise would contradict the note beside it (#128 review). */
        pedagogyPending:
          feedbackHtml === null && !(reveal && revealedOptionId === null),
        rung: reply.hint_rung_shown === undefined ? null : reply.hint_rung_shown,
        revealedOptionId: revealedOptionId,
        reveal: reveal
      };

      /* The verdict starts the celebration. It comes first because it is what
       * the learner is waiting for; the probe is the tutor's follow-up, and by
       * D13 its question is already in this same response. */
      if (reply.verdict === CORRECT) {
        view.celebrate(event);
      } else {
        view.showHint(event);
      }

      if (reply.blank_resolved) {
        resolved[blankId] = true;
        /* What goes in the gap. The service sends no resolved text on a
         * correct answer — there is no field for one — so it is what the
         * learner got right. On a reveal it is nothing at all: what they
         * submitted was wrong, and the revealed option would be asserting an
         * answer in the sentence they failed to complete (#112). The gap says
         * so rather than standing empty, so master acceptance 31's "silent
         * blank" is still not what a finished quiz shows. */
        view.resolveBlank({
          blankId: blankId,
          answer: event.reveal ? null : submitted,
          /* The words a closed gap shows, chosen here rather than in the DOM
           * half so that "it is not empty" is a decision under test. */
          closedText: event.reveal ? SEE_THE_NOTE : null
        });
        advance();
      }
      if (reply.probe) {
        view.showProbe({
          blankId: reply.probe.blank_id,
          questionHtml: orNull(reply.probe.question_html)
        });
      }
      if (reply.attempt_sealed) {
        view.showRating();
      }
    }

    function answerProbe(blankId, selfExplanation) {
      return guarded(
        send(PATHS.probes, {
          blank_id: blankId,
          self_explanation: selfExplanation
        }).then(function (reply) {
          view.hideProbe();
          view.probeGraded({
            blankId: blankId,
            verdict: orNull(reply.verdict),
            correctionHtml: orNull(reply.correction_html),
            /* Not each other's negation: a failed probe under
             * `CORRECT_AND_RESOLVE` re-opens nothing and leaves the blank
             * resolved, and so does one whose re-open cap is spent
             * (ADR-0009). */
            blankReopened: Boolean(reply.blank_reopened),
            blankResolved: Boolean(reply.blank_resolved),
            revealedOptionId: orNull(reply.revealed_option_id)
          });
          if (reply.blank_reopened) {
            resolved[blankId] = false;
            advance();
          } else if (reply.blank_resolved) {
            resolved[blankId] = true;
          }
          if (reply.attempt_sealed) {
            view.showRating();
          }
          return reply;
        })
      );
    }

    /* Dismissal is a null self-explanation on the same endpoint rather than a
     * fifth route, and it persists as a probe with no answer — which is why it
     * is a request at all and not a purely local hide (acceptance 18). */
    function dismissProbe(blankId) {
      return guarded(
        send(PATHS.probes, {
          blank_id: blankId,
          self_explanation: null
        }).then(function (reply) {
          view.hideProbe();
          if (reply && reply.attempt_sealed) {
            view.showRating();
          }
          return reply;
        })
      );
    }

    function submitRating(score) {
      return guarded(
        send(PATHS.ratings, { score: score }).then(function (reply) {
          view.hideRating();
          return reply;
        })
      );
    }

    /* Optional means optional. A learner who waves the prompt away leaves no
     * `RatingRecord`, and the sealed attempt is never written again. */
    function dismissRating() {
      view.hideRating();
    }

    return {
      token: function () {
        return token;
      },
      start: start,
      submitAnswer: submitAnswer,
      answerProbe: answerProbe,
      dismissProbe: dismissProbe,
      submitRating: submitRating,
      dismissRating: dismissRating
    };
  }

  /* --- The binding -------------------------------------------------------- */

  function setHtml(element, html) {
    if (!element) {
      return;
    }
    /* Every fragment set here left the service through `payloads.html_of` or
     * `content.render_explanation`, already sanitised against ADR-0012's
     * allowlist. The client renders no model-authored text of its own — which
     * is why the panels are authored in the document and only filled here. */
    element.innerHTML = html || "";
  }

  function show(element, visible) {
    if (element) {
      element.hidden = !visible;
    }
  }

  /* The DOM half: thin by construction, in the same sense the Pipe is. Every
   * decision has been taken out of it and put in the state machine above, which
   * is where the tests are. */
  function createDomView(root) {
    var verdict = root.querySelector(".socratic-verdict");
    var probe = root.querySelector(".socratic-probe");
    var rating = root.querySelector(".socratic-rating");
    var recap = root.querySelector(".socratic-recap");

    function placeholderFor(blankId) {
      return root.querySelector('.socratic-blank[data-blank-id="' + blankId + '"]');
    }

    function controlFor(blankId) {
      return root.querySelector(
        '.socratic-control[data-blank-id="' + blankId + '"]'
      );
    }

    function paint(event, kind) {
      show(verdict, true);
      verdict.setAttribute("data-verdict", kind);
      setHtml(
        verdict.querySelector(".socratic-verdict-line"),
        kind === CORRECT
          ? "That is it."
          : "Not yet" + (event.rung ? " — hint " + event.rung + " of 3" : "") + "."
      );
      setHtml(verdict.querySelector(".socratic-feedback"), event.feedbackHtml);
      setHtml(verdict.querySelector(".socratic-tutor-line"), event.tutorLineHtml);
      show(
        verdict.querySelector(".socratic-pedagogy-pending"),
        event.pedagogyPending
      );
      /* The note carries the whole disclosure, because the gap no longer
       * carries any of it (#112). It shows on every reveal, not only on one
       * with an option id: where the reveal is prose it arrives in the
       * feedback block directly above and carries no option id at all
       * (ADR-0003), and leaving the note hidden there would close the blank
       * with nothing telling the learner it is closed. */
      var reveal = verdict.querySelector(".socratic-reveal");
      if (event.reveal) {
        var option = event.revealedOptionId
          ? root.querySelector(
              '.socratic-option[data-option-id="' + event.revealedOptionId + '"]'
            )
          : null;
        setHtml(
          reveal,
          revealNote(option ? option.innerHTML : null, event.feedbackHtml !== null)
        );
      }
      show(reveal, Boolean(event.reveal));
    }

    return {
      celebrate: function (event) {
        paint(event, CORRECT);
      },
      showHint: function (event) {
        paint(event, "incorrect");
      },
      resolveBlank: function (event) {
        var placeholder = placeholderFor(event.blankId);
        if (placeholder) {
          if (event.answer === null) {
            /* Closed, and asserting nothing: the learner did not earn this
             * gap, and the note above it is where the answer is. A marker
             * rather than an empty span, so the finished quiz still has no
             * silent blank in it. */
            placeholder.setAttribute("data-state", "closed");
            placeholder.textContent = event.closedText;
          } else {
            placeholder.setAttribute("data-state", "resolved");
            var option = root.querySelector(
              '.socratic-option[data-option-id="' + event.answer + '"]'
            );
            if (option) {
              /* The option's label, already sanitised in the service, and
               * inline: `payloads.label_html` renders a phrase with no block
               * wrapper (#98), so what lands in this inline placeholder is
               * inline markup rather than a `<p>` with paragraph margins. */
              setHtml(placeholder, option.innerHTML);
            } else {
              /* Free text the learner typed. Text, never markup: it is the one
               * string in this document that did not come through the
               * service's sanitiser. */
              placeholder.textContent = event.answer;
            }
          }
        }
        show(controlFor(event.blankId), false);
      },
      activateBlank: function (blankId) {
        var placeholder = placeholderFor(blankId);
        if (placeholder) {
          placeholder.setAttribute("data-state", "active");
          placeholder.textContent = "";
        }
        show(controlFor(blankId), true);
      },
      allBlanksResolved: function () {
        show(recap, true);
      },
      showProbe: function (event) {
        setHtml(probe.querySelector(".socratic-probe-question"), event.questionHtml);
        probe.setAttribute("data-blank-id", event.blankId);
        show(probe, true);
      },
      hideProbe: function () {
        show(probe, false);
      },
      probeGraded: function (event) {
        setHtml(verdict.querySelector(".socratic-feedback"), event.correctionHtml);
        /* The correction replaces the feedback and nothing else, so without
         * this the reveal, the tutor line and the pending notice from whatever
         * was last graded stay on screen underneath it — a probe can be
         * answered long after the learner has moved on to another blank (#128
         * review). Only `paint` sets them, and it does not run here. */
        show(verdict.querySelector(".socratic-reveal"), false);
        setHtml(verdict.querySelector(".socratic-tutor-line"), null);
        show(verdict.querySelector(".socratic-pedagogy-pending"), false);
        if (event.blankReopened) {
          this.activateBlank(event.blankId);
        }
      },
      showRating: function () {
        show(recap, true);
        show(rating, true);
      },
      hideRating: function () {
        show(rating, false);
      },
      failed: function (error) {
        show(verdict, true);
        verdict.removeAttribute("data-verdict");
        setHtml(
          verdict.querySelector(".socratic-verdict-line"),
          "That did not reach the tutor. Try again."
        );
        if (typeof console !== "undefined" && console.warn) {
          console.warn(String(error));
        }
      }
    };
  }

  /* Read the configuration off the root element, wire the client, and bind the
   * controls. The walk over the blanks is in declared order — the order the
   * segment walk drew them, which is also the order the final blank is the last
   * of (acceptance 19). One blank is active at a time. */
  function mount(doc) {
    var root = doc.querySelector(".socratic-overlay");
    if (!root || !root.getAttribute("data-capability-token")) {
      return null;
    }

    var view = createDomView(root);
    var controls = Array.prototype.slice.call(
      root.querySelectorAll(".socratic-control")
    );
    var client = createQuizClient({
      blankIds: controls.map(function (control) {
        return control.getAttribute("data-blank-id");
      }),
      sessionId: root.getAttribute("data-quiz-session-id"),
      token: root.getAttribute("data-capability-token"),
      transport: createFetchTransport(
        doc.defaultView.fetch.bind(doc.defaultView),
        root.getAttribute("data-service-base-url")
      ),
      view: view
    });

    controls.forEach(function (control) {
      var blankId = control.getAttribute("data-blank-id");

      control.querySelectorAll(".socratic-option").forEach(function (button) {
        button.addEventListener("click", function () {
          client.submitAnswer(blankId, button.getAttribute("data-option-id"));
        });
      });

      var field = control.querySelector(".socratic-answer");
      var submit = control.querySelector(".socratic-submit");
      if (submit && field) {
        submit.addEventListener("click", function () {
          client.submitAnswer(blankId, field.value);
        });
      }
    });

    var probe = root.querySelector(".socratic-probe");
    probe
      .querySelector(".socratic-probe-submit")
      .addEventListener("click", function () {
        var answer = probe.querySelector(".socratic-probe-answer");
        client.answerProbe(probe.getAttribute("data-blank-id"), answer.value);
        answer.value = "";
      });
    probe
      .querySelector(".socratic-probe-dismiss")
      .addEventListener("click", function () {
        client.dismissProbe(probe.getAttribute("data-blank-id"));
      });

    var rating = root.querySelector(".socratic-rating");
    rating.querySelectorAll(".socratic-score").forEach(function (button) {
      button.addEventListener("click", function () {
        client.submitRating(Number(button.getAttribute("data-score")));
      });
    });
    rating
      .querySelector(".socratic-rating-dismiss")
      .addEventListener("click", function () {
        client.dismissRating();
      });

    /* The first blank is where the learner starts; the client reveals the rest
     * as each one resolves, so the panel shows one control at a time. */
    client.start();

    return client;
  }

  return {
    createFetchTransport: createFetchTransport,
    revealNote: revealNote,
    createQuizClient: createQuizClient,
    createDomView: createDomView,
    mount: mount
  };
})();
