/* Keeps a MASEO web app page live. FastAPI makes every page (app.py,
   templates/index.html); this script has no logic of the app. It only
   (1) makes every running time (an element with data-since) tick each
       second, and
   (2) on a page with data-live="<seconds>" asks FastAPI for the same page
       again every few seconds and swaps in the blocks marked data-swap
       that changed. The page keeps its scroll position, open step files
       and the log's scroll position, and nothing is swapped while you are
       selecting text.
   Without JavaScript the page reloads itself instead (<noscript> refresh). */
(function () {
  "use strict";
  var offset = 0;          // the server's clock minus this browser's clock
  var timer = null;
  var busy = false;

  function setClock(doc) {
    var now = parseFloat(doc.body.getAttribute("data-now") || "");
    if (now) offset = now - Date.now() / 1000;
  }

  // the same format as views.dur(): 7s / 3m 05s / 1h 04m
  function dur(s) {
    s = Math.max(0, Math.round(s));
    var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
    var two = function (n) { return (n < 10 ? "0" : "") + n; };
    return h ? h + "h " + two(m) + "m" : m ? m + "m " + two(x) + "s" : x + "s";
  }

  function tick() {
    var now = Date.now() / 1000 + offset;
    var els = document.querySelectorAll("[data-since]");
    for (var i = 0; i < els.length; i++) {
      var since = parseFloat(els[i].getAttribute("data-since"));
      if (since) els[i].textContent = (els[i].getAttribute("data-prefix") || "") + dur(now - since);
    }
  }

  function keys(root) {
    var out = [], els = root.querySelectorAll("[data-swap]");
    for (var i = 0; i < els.length; i++) out.push(els[i].getAttribute("data-swap"));
    return out.join("|");
  }

  // what a block shows, apart from its ticking times (those change every
  // second anyway): a block is swapped only when this differs
  function signature(el) {
    return el.innerHTML.replace(/(data-since="[^"]*"[^>]*>)[^<]*/g, "$1");
  }

  function selecting() {
    var sel = window.getSelection && window.getSelection();
    return sel && !sel.isCollapsed && String(sel).trim() !== "";
  }

  function swap(doc) {
    if (keys(document.body) !== keys(doc.body)) {
      // another kind of page (a job appeared, ended in another layout, ...)
      document.body.innerHTML = doc.body.innerHTML;
      return;
    }
    var fresh = doc.querySelectorAll("[data-swap]");
    for (var i = 0; i < fresh.length; i++) {
      var key = fresh[i].getAttribute("data-swap");
      if (fresh[i].querySelector("[data-swap]")) continue;       // only the innermost blocks
      var old = document.querySelector('[data-swap="' + key + '"]');
      if (!old || signature(old) === signature(fresh[i])) continue;
      var log = old.querySelector(".logbox"), logTop = log ? log.scrollTop : 0;
      old.innerHTML = fresh[i].innerHTML;
      var newLog = old.querySelector(".logbox");
      if (newLog) newLog.scrollTop = logTop;                      // 0 = the end of the log
    }
  }

  function schedule(seconds) {
    clearTimeout(timer);
    if (seconds > 0) timer = setTimeout(update, seconds * 1000);
  }

  function every() {
    return parseFloat(document.body.getAttribute("data-live") || "0");
  }

  function update() {
    if (busy) return;
    if (document.hidden || selecting()) { schedule(every()); return; }
    busy = true;
    fetch(location.href, { cache: "no-store", credentials: "same-origin" })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.text(); })
      .then(function (html) {
        var doc = new DOMParser().parseFromString(html, "text/html");
        setClock(doc);
        swap(doc);
        document.title = doc.title;
        // the server says how often to update (none: e.g. nothing is live)
        document.body.setAttribute("data-live", doc.body.getAttribute("data-live") || "");
        document.body.setAttribute("data-now", doc.body.getAttribute("data-now") || "");
        tick();
      })
      .catch(function () { /* the server did not answer: try again later */ })
      .then(function () { busy = false; schedule(every() || 0); });
  }

  function start() {
    setClock(document);
    tick();
    setInterval(tick, 1000);
    schedule(every());
    // back to the tab: update at once instead of at the next turn
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden && every()) update();
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
