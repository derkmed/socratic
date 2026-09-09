/* The overlay's client: one classic script assigning one global.
 *
 * Classic and single-global on purpose. The browser gets `window.SocraticQuiz`
 * from an inline `<script>` in a `srcdoc` document with an opaque origin, where
 * a module's import machinery has nothing to resolve against; and a child Node
 * interpreter can evaluate these same bytes and pull the factory out, which is
 * how `tests/test_ui_client.py` drives the state machine with no DOM and no
 * network.
 *
 * Nothing runs at load time. `mount` is called by the document.
 */
var SocraticQuiz = (function () {
  "use strict";

  function mount(doc) {
    return null;
  }

  return { mount: mount };
})();
