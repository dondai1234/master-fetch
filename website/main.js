/* ============================================================
   HOUND — main.js
   Vanilla JS. IntersectionObserver reveals + count-up,
   copy buttons, nav active highlight, mobile menu, hero path draw.
   ============================================================ */
(function () {
  "use strict";

  var prefersReduced =
    window.matchMedia &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ---------- helper: reveal once ---------- */
  function revealOnce(selector, opts) {
    var els = document.querySelectorAll(selector);
    if (!els.length) return;
    if (prefersReduced || !("IntersectionObserver" in window)) {
      els.forEach(function (el) { el.classList.add("visible"); });
      if (opts && opts.onShow) {
        els.forEach(function (el) { opts.onShow(el); });
      }
      return;
    }
    var io = new IntersectionObserver(
      function (entries) {
        entries.forEach(function (entry) {
          if (entry.isIntersecting) {
            entry.target.classList.add("visible");
            if (opts && opts.onShow) opts.onShow(entry.target);
            io.unobserve(entry.target);
          }
        });
      },
      { threshold: 0.25, rootMargin: "0px 0px -8% 0px" }
    );
    els.forEach(function (el) { io.observe(el); });
  }

  /* ---------- clip-path line reveals ---------- */
  revealOnce(".clip-reveal");

  /* ---------- hero tracking path: draw on load ---------- */
  var track = document.getElementById("hero-track");
  if (track) {
    if (prefersReduced) {
      track.classList.add("drawn");
      document.querySelectorAll(".waypoint").forEach(function (w) { w.classList.add("shown"); });
    } else {
      // slight delay so fonts/layout settle
      window.addEventListener("load", function () {
        requestAnimationFrame(function () {
          track.classList.add("drawn");
          // reveal waypoints staggered as the path draws
          var wps = document.querySelectorAll(".waypoint");
          var step = 2000 / (wps.length + 1);
          wps.forEach(function (w, i) {
            setTimeout(function () { w.classList.add("shown"); }, step * (i + 1));
          });
        });
      });
    }
  }

  /* ---------- radial diagram: draw on scroll-in ---------- */
  var radial = document.getElementById("radial-diagram");
  if (radial) {
    if (prefersReduced) {
      radial.classList.add("drawn");
    } else if ("IntersectionObserver" in window) {
      var rio = new IntersectionObserver(
        function (entries) {
          entries.forEach(function (entry) {
            if (entry.isIntersecting) {
              entry.target.classList.add("drawn");
              rio.unobserve(entry.target);
            }
          });
        },
        { threshold: 0.4 }
      );
      rio.observe(radial);
    } else {
      radial.classList.add("drawn");
    }
  }

  /* ---------- count-up stats ---------- */
  function countUp(el, target, duration, prefix, suffix) {
    if (prefersReduced) {
      el.textContent = (prefix || "") + target + (suffix || "");
      return;
    }
    var start = performance.now();
    function tick(now) {
      var progress = Math.min((now - start) / duration, 1);
      var eased = 1 - Math.pow(1 - progress, 3); // ease-out cubic
      el.textContent = (prefix || "") + Math.round(eased * target) + (suffix || "");
      if (progress < 1) requestAnimationFrame(tick);
    }
    requestAnimationFrame(tick);
  }

  (function setupCountUp() {
    var nums = document.querySelectorAll("[data-count]");
    if (!nums.length) return;
    if (prefersReduced || !("IntersectionObserver" in window)) {
      nums.forEach(function (el) {
        var target = parseInt(el.getAttribute("data-count"), 10) || 0;
        var prefix = el.getAttribute("data-prefix") || "";
        var suffix = el.getAttribute("data-suffix") || "";
        el.textContent = prefix + target + suffix;
      });
      return;
    }
    var cio = new IntersectionObserver(
      function (entries) {
        entries.forEach(function (entry) {
          if (entry.isIntersecting) {
            var el = entry.target;
            var target = parseInt(el.getAttribute("data-count"), 10) || 0;
            var prefix = el.getAttribute("data-prefix") || "";
            var suffix = el.getAttribute("data-suffix") || "";
            countUp(el, target, 1800, prefix, suffix);
            cio.unobserve(el);
          }
        });
      },
      { threshold: 0.6 }
    );
    nums.forEach(function (el) { cio.observe(el); });
  })();

  /* ---------- copy buttons (Clipboard API) ---------- */
  (function setupCopy() {
    var btns = document.querySelectorAll(".copy-btn[data-copy-target]");
    btns.forEach(function (btn) {
      btn.addEventListener("click", function () {
        var id = btn.getAttribute("data-copy-target");
        var codeEl = document.getElementById(id);
        if (!codeEl) return;
        var text = codeEl.textContent.trim();
        function flash() {
          btn.classList.add("copied");
          setTimeout(function () { btn.classList.remove("copied"); }, 1600);
        }
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text).then(flash).catch(function () {
            legacyCopy(codeEl); flash();
          });
        } else {
          legacyCopy(codeEl); flash();
        }
      });
    });

    function legacyCopy(el) {
      var range = document.createRange();
      range.selectNodeContents(el);
      var sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      try { document.execCommand("copy"); } catch (e) {}
      sel.removeAllRanges();
    }
  })();

  /* ---------- nav active section highlight ---------- */
  (function setupNavActive() {
    var links = document.querySelectorAll(".nav-links a[data-target]");
    if (!links.length || !("IntersectionObserver" in window)) return;
    var byId = {};
    links.forEach(function (a) {
      var id = a.getAttribute("data-target");
      byId[id] = a;
    });
    var nio = new IntersectionObserver(
      function (entries) {
        entries.forEach(function (entry) {
          if (entry.isIntersecting) {
            var id = entry.target.id;
            links.forEach(function (l) { l.classList.remove("active"); });
            if (byId[id]) byId[id].classList.add("active");
          }
        });
      },
      { rootMargin: "-45% 0px -50% 0px", threshold: 0 }
    );
    Object.keys(byId).forEach(function (id) {
      var sec = document.getElementById(id);
      if (sec) nio.observe(sec);
    });
  })();

  /* ---------- mobile menu ---------- */
  (function setupMobileMenu() {
    var toggle = document.getElementById("nav-toggle");
    var list = document.getElementById("nav-links");
    if (!toggle || !list) return;
    toggle.addEventListener("click", function () {
      var open = list.classList.toggle("open");
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    });
    // close on link click
    list.querySelectorAll("a").forEach(function (a) {
      a.addEventListener("click", function () {
        list.classList.remove("open");
        toggle.setAttribute("aria-expanded", "false");
      });
    });
    // close on outside click
    document.addEventListener("click", function (e) {
      if (!list.classList.contains("open")) return;
      if (!list.contains(e.target) && !toggle.contains(e.target)) {
        list.classList.remove("open");
        toggle.setAttribute("aria-expanded", "false");
      }
    });
  })();

  /* ---------- hide hero bg img if it 404s ---------- */
  (function setupImgFallback() {
    var img = document.querySelector(".hero-bg-img");
    if (!img) return;
    img.addEventListener("error", function () { img.style.display = "none"; });
  })();

  /* ---------- smooth-scroll offset for fixed nav ---------- */
  (function setupAnchorOffset() {
    document.querySelectorAll('a[href^="#"]').forEach(function (a) {
      a.addEventListener("click", function (e) {
        var href = a.getAttribute("href");
        if (href === "#" || href.length < 2) return;
        var target = document.querySelector(href);
        if (!target) return;
        e.preventDefault();
        var y = target.getBoundingClientRect().top + window.pageYOffset - 64;
        window.scrollTo({ top: y, behavior: prefersReduced ? "auto" : "smooth" });
      });
    });
  })();
})();
