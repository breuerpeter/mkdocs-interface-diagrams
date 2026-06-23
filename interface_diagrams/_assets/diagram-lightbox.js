// Diagram overlay + diagram-to-diagram navigation.
//
//  * Click the inlined system diagram (or any diagram title — a device /
//    component / interface / flow heading, which is itself the diagram link) to
//    expand that diagram into a full-viewport overlay.
//  * Click a link INSIDE a diagram (inline or expanded): every such link targets
//    a documented section, and every section with a diagram exposes one. So
//    instead of just scrolling there, we open that section's diagram directly in
//    the overlay and navigate the page to the section behind it — letting you
//    drill through the system diagram-by-diagram. Cross-page targets reload
//    (no instant nav), so the intent is stashed and replayed on the next load.
(function () {
  var INTENT = "diagram-autoopen"; // sessionStorage key: "<pathname>#<anchor>"

  // Serialize an SVG element to a standalone file and trigger its download.
  function downloadSvg(svg, name) {
    var clone = svg.cloneNode(true);
    clone.setAttribute("xmlns", "http://www.w3.org/2000/svg");
    var data = new XMLSerializer().serializeToString(clone);
    var url = URL.createObjectURL(
      new Blob([data], { type: "image/svg+xml;charset=utf-8" }));
    var a = document.createElement("a");
    a.href = url;
    a.download = name || "diagram.svg";
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  }

  // A toolbar icon button; `path` is the `d` of a 24x24 Material glyph.
  function iconButton(title, path, onClick) {
    var btn = document.createElement("button");
    btn.className = "diagram-tool";
    btn.title = title;
    btn.setAttribute("aria-label", title);
    btn.innerHTML =
      '<svg viewBox="0 0 24 24" width="24" height="24" aria-hidden="true">' +
      '<path fill="currentColor" d="' + path + '"/></svg>';
    btn.addEventListener("click", function (e) {
      e.stopPropagation(); // don't let the overlay's close handler fire
      onClick();
    });
    return btn;
  }

  // Absolute URL of the top-level system diagram, from the page-relative href
  // the build hook exposes (see hooks.py); null on pages without one. Resolved
  // to absolute so openFromUrl can use it as a base when rewriting the SVG's
  // own links (a relative base there throws → the new-tab fallback fires).
  function systemDiagramUrl() {
    var el = document.querySelector(".system-diagram-link");
    return el && new URL(el.getAttribute("data-url"), location.href).href;
  }

  function openLightbox(svg, name) {
    var box = document.createElement("div");
    box.className = "diagram-lightbox";
    // importNode so an SVG parsed from a fetched document is adopted into this
    // page; for an inline SVG it just clones (the original stays in the prose).
    var clone = document.importNode(svg, true);
    clone.removeAttribute("style"); // drop the inline max-width:100% so it can grow
    box.appendChild(clone);

    var bar = document.createElement("div");
    bar.className = "diagram-toolbar";
    // A click on the toolbar's own gap/padding shouldn't close the overlay.
    bar.addEventListener("click", function (e) { e.stopPropagation(); });
    var home = systemDiagramUrl();
    if (home)
      bar.appendChild(iconButton("Back to system diagram",
        "M10,20V14H14V20H19V12H22L12,3L2,12H5V20H10Z",
        function () { openFromUrl(home); }));
    bar.appendChild(iconButton("Download diagram",
      "M5,20H19V18H5M19,9H15V3H9V9H5L12,16L19,9Z",
      function () { downloadSvg(clone, name); }));
    box.appendChild(bar);

    document.body.appendChild(box);
  }

  // Inline diagrams have no source URL; name the download after the page slug.
  function inlineName() {
    var segs = location.pathname.split("/").filter(Boolean);
    return (segs.pop() || "diagram") + ".svg";
  }

  function closeLightbox() {
    var lb = document.querySelector(".diagram-lightbox");
    if (lb) lb.remove();
  }

  function openFromUrl(url) {
    fetch(url)
      .then(function (r) { return r.text(); })
      .then(function (text) {
        var svg = new DOMParser()
          .parseFromString(text, "image/svg+xml")
          .querySelector("svg");
        if (!svg) { window.open(url, "_blank", "noopener"); return; }
        // The standalone SVG's links are relative to ITS url; resolve them to
        // absolute so they still work from whatever page hosts the overlay.
        svg.querySelectorAll("a").forEach(function (a) {
          ["href", "xlink:href"].forEach(function (attr) {
            var v = a.getAttribute(attr);
            if (v) a.setAttribute(attr, new URL(v, url).href);
          });
        });
        closeLightbox();
        openLightbox(svg, url.split("/").pop().split(/[?#]/)[0]);
      })
      .catch(function () { window.open(url, "_blank", "noopener"); });
  }

  // The diagram for the section headed by `anchor` on THIS page. The heading's
  // own text is usually the diagram link (the title opens its diagram); a flow
  // label carries it likewise. Fall back to scanning the section body for a link
  // or an inlined SVG. null when the section has no diagram.
  function sectionDiagram(anchor) {
    var h = anchor && document.getElementById(anchor);
    if (!h) return null;
    var own = h.querySelector && h.querySelector("a.diagram-link");
    if (own) return { link: own.href };
    for (var el = h.nextElementSibling; el; el = el.nextElementSibling) {
      if (/^H[1-6]$/.test(el.tagName)) break;      // next section — stop
      var link = el.matches && el.matches("a.diagram-link")
        ? el : el.querySelector && el.querySelector("a.diagram-link");
      if (link) return { link: link.href };
      var svg = el.matches && el.matches(".interface-diagram")
        ? el.querySelector("svg")
        : el.querySelector && el.querySelector(".interface-diagram svg");
      if (svg) return { svg: svg };
    }
    return null;
  }

  function openSection(anchor) {
    var d = sectionDiagram(anchor);
    // No anchor → the page's top-level inlined subsystem/system diagram, which
    // is embedded ABOVE the first heading (so it lives under no section and
    // sectionDiagram can't find it). A subsystem title links here without a
    // fragment — e.g. clicking "Skynode" in a device diagram on the Skynode
    // page. Open that inline diagram instead of no-opping. Device/component
    // diagrams are never inlined (they're `a.diagram-link`), so the first
    // `.interface-diagram` on the page is reliably the subsystem/system one.
    if (!d && !anchor) {
      var top = document.querySelector(".interface-diagram svg");
      if (top) d = { svg: top };
    }
    if (!d) return false;
    if (d.link) openFromUrl(d.link);
    else openLightbox(d.svg, inlineName());
    return true;
  }

  function aHref(a) { return a.getAttribute("href") || a.getAttribute("xlink:href"); }

  // A link inside a diagram was clicked: go to its section's diagram.
  function followDiagramLink(href) {
    var url = new URL(href, location.href);
    // Payload-token / title links target a diagram .svg directly — open it in
    // the overlay rather than navigating to the file (no page navigation).
    if (/\.svg$/i.test(url.pathname)) { openFromUrl(url.href); return; }
    var anchor = decodeURIComponent((url.hash || "").replace(/^#/, ""));
    if (url.pathname === location.pathname) {
      closeLightbox();
      if (anchor) location.hash = anchor; // scroll the page behind the overlay
      openSection(anchor);
    } else {
      // Different page: full reload, so stash intent and replay it on arrival.
      sessionStorage.setItem(INTENT, url.pathname + "#" + anchor);
      location.href = url.href;
    }
  }

  document.addEventListener("click", function (e) {
    // 1. A link inside a diagram (inline SVG or the expanded overlay). Handle it
    //    here even if it has no href, so a malformed link never falls through to
    //    the close branch and dismisses the overlay.
    var inDia = e.target.closest(".diagram-lightbox a, .interface-diagram a");
    if (inDia) {
      var href = aHref(inDia);
      if (href) { e.preventDefault(); followDiagramLink(href); }
      return;
    }
    // 2. Any other click while the overlay is open closes it.
    if (document.querySelector(".diagram-lightbox")) { closeLightbox(); return; }
    // 3. A diagram title in the prose (a heading / flow-label link) → open it.
    var openLink = e.target.closest("a.diagram-link");
    if (openLink) { e.preventDefault(); openFromUrl(openLink.href); return; }
    // 4. Click an inlined diagram (not a link) to expand it.
    if (e.target.closest("a")) return;
    var dia = e.target.closest(".interface-diagram");
    if (dia) { var svg = dia.querySelector("svg"); if (svg) openLightbox(svg, inlineName()); }
  });

  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") closeLightbox();
  });

  // On arrival from a cross-page diagram link, open the target section's diagram.
  function replayIntent() {
    var want = sessionStorage.getItem(INTENT);
    if (!want) return;
    sessionStorage.removeItem(INTENT);
    var hash = want.indexOf("#") >= 0 ? want.slice(want.indexOf("#") + 1) : "";
    if (want.slice(0, want.indexOf("#")) === location.pathname) openSection(hash);
  }
  if (document.readyState === "loading")
    document.addEventListener("DOMContentLoaded", replayIntent);
  else
    replayIntent();
})();
