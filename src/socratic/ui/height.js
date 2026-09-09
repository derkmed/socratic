/* The document tells its frame how tall it is (#116).
 *
 * Open WebUI sizes an embed by reading `contentDocument.scrollHeight` first
 * and falling back to a `postMessage` from inside. Every frame ours renders in
 * is `srcdoc`, sandboxed without `allow-same-origin`, so the first path throws
 * on an opaque origin and this message is the only one that arrives. A
 * document that stays quiet renders at no height at all.
 *
 * Inlined like `client.js` and `overlay.css`: no external reference of any
 * kind (ADR-0012).
 */
(function () {
  var last = 0;

  /* The measure is over the body's CHILDREN, never over `documentElement` or
   * `body` themselves.
   *
   * Those two report the height the parent just gave the frame — set the frame
   * to H and their `scrollHeight` *is* H — so reporting one feeds our own
   * output back in, and any body padding is added again on every pass. That
   * ratchets without bound; it was measured doing so before this was written.
   * A child's own box does not grow when the frame does. */
  function contentHeight() {
    var body = document.body;
    if (!body) {
      return 0;
    }
    var bottom = 0;
    for (var i = 0; i < body.children.length; i++) {
      var child = body.children[i];
      var edge = child.offsetTop + child.offsetHeight;
      if (edge > bottom) {
        bottom = edge;
      }
    }
    var style = window.getComputedStyle(body);
    bottom += parseFloat(style.paddingBottom || 0);
    bottom += parseFloat(style.marginBottom || 0);
    return Math.ceil(bottom);
  }

  function report() {
    var height = contentHeight();
    /* Only on a real change, so a settled layout stops sending. The 4px
     * threshold absorbs sub-pixel rounding, which would otherwise post on
     * every pass forever. */
    if (height > 0 && Math.abs(height - last) > 4) {
      last = height;
      parent.postMessage({ type: "iframe:height", height: height }, "*");
    }
  }

  window.addEventListener("load", report);
  /* The quiz changes height as the learner fills blanks and panels appear, and
   * none of that fires `load`. */
  if (window.ResizeObserver && document.body) {
    var observer = new ResizeObserver(report);
    observer.observe(document.body);
    for (var i = 0; i < document.body.children.length; i++) {
      observer.observe(document.body.children[i]);
    }
  }
  /* Belt and braces for the first paint, before any observer has fired. */
  setTimeout(report, 50);
  setTimeout(report, 500);
})();
