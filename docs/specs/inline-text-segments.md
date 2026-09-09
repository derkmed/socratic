# A text segment is a clause, not a paragraph

## Goal

`content.render_segment` sends every `TextSegment` through
`markdown.render_markdown`, which is a **block** render: it wraps the text in
`<p>` and drops the leading and trailing spaces. A text segment is usually a
*fragment of a sentence* with a blank on one or both sides (CONTEXT: Segment;
[ADR-0004](../adr/0004-quiz-wire-format.md)), so the walk emits a stack of
paragraphs where one flowing line was meant, and the words on either side of a
blank lose the space between them.

The overlay compensates presentationally — `.socratic-explanation > p { display:
inline }` in `src/socratic/ui/overlay.css` — which flattens a *real* paragraph
break inside a single text segment along with the artificial ones.

Render a text segment **inline** when it is a clause, and keep block rendering
only for a segment that genuinely carries block content. Then delete the CSS
workaround, because the paragraphs that remain in the output are ones that ought
to be paragraphs. Closes #86.

## Seams

| Seam | Kind | Why it must exist |
|---|---|---|
| `markdown.render_fragment(text) -> str` | new pure function | The whole decision lives here: whether this text is a clause or block content, and therefore whether it gets a `<p>`. It is a `markdown` function and not a `content` one because the decision is made from the parser's own block token stream — `content` must not learn what a `markdown-it` token is. Sanitised like everything else the module emits. |
| `content.render_segment(segment) -> str` | existing | The dispatch point. The change is one line: `TextSegment` goes to `render_fragment` instead of `render_markdown`. |
| `content.render_explanation(explanation) -> str` | existing | Where the issue's complaint is checkable end to end: text–blank–text renders as one flowing line with its spaces intact and no `<p>`. This is the acceptance seam. |
| `markdown.render_markdown(text) -> str` | existing, unchanged | Still the block render, still what `service.payloads.html_of` calls for an option's `text_html`. Not touched. |

## Decisions

- **`renderInline` is the mechanism.** `markdown-it-py`'s `renderInline` runs
  the inline rules only, emits no `<p>`, and — verified against the repo's own
  parser — preserves leading and trailing whitespace:
  `_PARSER.renderInline('Heat flows because ')` is `'Heat flows because '`,
  where `render` gives `'<p>Heat flows because</p>\n'`. That is both halves of
  the issue's complaint fixed by one call. The same parser instance is used, so
  the restricted subset, `html: False` escaping and `linkify: False` all carry
  over unchanged.

- **Inline is not safe for every segment, so the choice is per segment.** Under
  `renderInline` a list renders as its literal `- ` characters and a fenced
  block is mis-parsed by the inline backticks rule into a single-line `<code>`:
  `` '```python\nx = 1\n```' `` becomes `` '<code>python x = 1 </code>' ``. A
  blanket switch would be a regression, not a fix.

- **The parser decides, by its own block token stream.** `render_fragment`
  parses the text with the block parser and takes the inline path when the
  result is **at most one paragraph and nothing else** — `[]`, or exactly
  `paragraph_open, inline, paragraph_close`. Anything else (a list, a fence,
  two paragraphs) is block content and renders through `render_markdown`
  unchanged. This is a question about the text, answered by the thing that
  already knows how to answer it, rather than by a regex guessing at block
  markers.

- **A segment carrying block content is a paragraph, and that is correct.** The
  issue raises this as the open question. A fenced code block or a bulleted list
  is not a clause with a blank on either side; it is a block, and drawing it as
  one is what the reader wants. The wire format is not extended with a
  per-segment "inline or block" flag: ADR-0004's segment array is deliberately
  minimal, the text itself already answers the question, and a flag would let an
  author declare a fence inline — a state with no sensible rendering.

- **The empty token stream takes the inline path.** A whitespace-only text
  segment (`' '`, the separator between two adjacent blanks) block-renders to
  the empty string, which fuses the two blanks together. Inline it stays a
  space. `''` renders to `''` either way.

- **The CSS workaround comes out, and is genuinely subsumed.** After this
  change the only `<p>` a direct child of `.socratic-explanation` can be is one
  the author actually wrote as a paragraph, which is exactly what the rule was
  destroying. `.socratic-explanation > p { display: inline; margin: 0 }` and its
  comment are deleted; no test asserts them, and no other rule or client-side
  selector depends on them.

- **`payloads.html_of` and option `text_html` stay block.** An option's label is
  rendered by a different path for a different purpose, and `docs/specs/quiz-ui.md`
  reasons explicitly from its being block-level. Changing it is not needed to
  close this issue and would move the blast radius into the control panel.

## Approach

Three red-green loops, then the cleanup.

1. **`markdown.render_fragment`** — the inline path. Red: a clause renders with
   no `<p>` and keeps its outer spaces. Green: `renderInline`, sanitised.
2. **`markdown.render_fragment`** — the block path. Red: a fence, a list and a
   two-paragraph text each render exactly as `render_markdown` renders them.
   Green: the token-stream test and the fallback.
3. **`content.render_segment`** — the walk. Red: the issue's own reproduction —
   `('Heat flows because ', blank, ' rises.')` renders as one line with both
   spaces and no `<p>`. Green: one line changed.
4. Delete the CSS rule and its comment; update `docs/specs/quiz-ui.md` and
   `docs/specs/content-rendering.md` where they describe the old behaviour.

## Out of scope

- **Option `text_html`, and the `<p>` the client copies into a resolved blank.**
  A separate path with a separate justification; filed separately.
- **Adding a segment-level inline/block flag to the wire format** (ADR-0004).
- **Nested blanks**, settled out of scope by ADR-0012 and structurally
  impossible in `domain/types.py`.
- **The Markdown subset itself** (ADR-0012). No rule is enabled or disabled;
  `ENABLED_RULES` is byte-identical.
- **`sanitiser.py`, `mathml.py`, `overlay.py`, `client.js`.** Untouched.

## Acceptance

1. `content.render_explanation((TextSegment('Heat flows because '),
   BlankSegment('b1'), TextSegment(' rises.')))` contains no `<p>` and reads
   `Heat flows because <span …></span> rises.` — the exact case in the issue.
2. A text segment that is a fenced code block still renders `<pre><code
   class="language-…">` with Pygments spans; one that is a list still renders
   `<ul><li>`; one holding two paragraphs still renders two `<p>` elements.
3. `render_fragment` output is sanitised: `<script>` in a text segment is
   escaped, and no `javascript:` href survives.
4. `.socratic-explanation > p` does not appear in `src/socratic/ui/overlay.css`.
5. Full suite green, no skips gained.
