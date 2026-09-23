/* Motion helper.
   CSS cannot tell when an image has finished decoding, so this marks each one
   as it arrives and lets the stylesheet fade it in. Deliberately tiny, and
   wrapped so that a failure here can never take the app down with it. */
(function () {
  "use strict";
  try {
    if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

    var ready = function (img) {
      if (img.getAttribute("data-zk-ready") === null) img.setAttribute("data-zk-ready", "");
    };

    var watch = function (img) {
      if (!img || img.getAttribute("data-zk-img") !== null) return;
      // Skip the tiny inline ones - a fading icon reads as a glitch.
      var w = img.getAttribute("width");
      if (w && Number(w) < 40) return;
      img.setAttribute("data-zk-img", "");
      // A cached image is already complete by the time we see it; fade it in
      // on the next frame so the transition still has two states to run over.
      if (img.complete && img.naturalWidth) {
        requestAnimationFrame(function () { ready(img); });
      } else {
        img.addEventListener("load", function () { ready(img); }, { once: true });
        // A broken image must not be left invisible.
        img.addEventListener("error", function () { ready(img); }, { once: true });
        // Belt and braces: the stylesheet hides an image until it is marked
        // ready, so anything that never fires either event - a decode that
        // stalls, a listener attached a moment too late - would stay invisible
        // for good. Nothing stays hidden longer than this.
        setTimeout(function () { ready(img); }, 5000);
      }
    };

    var scan = function (root) {
      if (!root || root.nodeType !== 1) return;
      if (root.tagName === "IMG") watch(root);
      var list = root.querySelectorAll ? root.querySelectorAll("img") : [];
      for (var i = 0; i < list.length; i++) watch(list[i]);
    };

    var start = function () {
      scan(document.body);
      if (!window.MutationObserver) return;
      new MutationObserver(function (records) {
        for (var i = 0; i < records.length; i++) {
          var added = records[i].addedNodes;
          for (var j = 0; j < added.length; j++) scan(added[j]);
        }
      }).observe(document.body, { childList: true, subtree: true });
    };

    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", start);
    } else {
      start();
    }
  } catch (e) {
    /* Motion is decoration. If it cannot run, the app still works. */
  }
})();
