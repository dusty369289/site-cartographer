// site-cartographer viewer
// Loads graph.json, renders Cytoscape graph, opens MHTML in iframe on node click,
// highlights clickable elements (a, area) by injecting CSS / canvas overlay.

(function () {
  const RUN_BASE = "../"; // graph.json, pages/, thumbs/ live one level up from viewer/
  let cy;
  let highlightsOn = true;
  let edgesByPage = new Map();

  fetch(RUN_BASE + "graph.json")
    .then((r) => {
      if (!r.ok) throw new Error("graph.json: HTTP " + r.status);
      return r.json();
    })
    .then(initGraph)
    .catch((err) => {
      document.getElementById("panel-title").textContent = "viewer error: " + err.message;
      console.error(err);
    });

  function initGraph(graph) {
    indexEdgesByPage(graph);

    const elements = [
      ...graph.nodes.map(toNode),
      ...graph.edges,
    ];

    // Pick a layout based on graph size. cose looks great but is O(n²) per
    // iteration — too slow for >300 nodes. concentric grouped by depth is
    // near-instant and still readable for large graphs.
    const nodeCount = graph.nodes.length;
    const layoutSpec = nodeCount > 300
      ? {
          name: "concentric",
          concentric: (n) => -(n.data("depth") ?? 99),
          levelWidth: () => 1,
          minNodeSpacing: 25,
          animate: false,
          padding: 30,
        }
      : { name: "cose", animate: false, nodeRepulsion: 8000, idealEdgeLength: 100 };

    try {
      cy = cytoscape({
        container: document.getElementById("cy"),
        elements: elements,
        style: [
          {
            selector: "node",
            style: {
              "background-color": "#444",
              "background-fit": "cover",
              "background-opacity": 1,
              width: 60,
              height: 45,
              label: "data(short_label)",
              color: "#ddd",
              "font-size": 8,
              "text-valign": "bottom",
              "text-margin-y": 4,
              "text-wrap": "ellipsis",
              "text-max-width": "70px",
              "border-width": 1,
              "border-color": "#666",
            },
          },
          {
            selector: "node[thumb_url]",
            style: {
              "background-image": "data(thumb_url)",
            },
          },
          {
            selector: "node[?is_external]",
            style: { "background-color": "#552", "border-color": "#aa6", "shape": "diamond" },
          },
          {
            selector: "node[?is_unvisited]",
            style: { "background-color": "#333", "border-color": "#555", "border-style": "dashed" },
          },
          {
            selector: "node[?is_phantom_404]",
            style: { "background-color": "#522", "border-color": "#a66" },
          },
          {
            selector: "node[?is_duplicate]",
            style: { "border-color": "#88a", "border-style": "dashed", "opacity": 0.7 },
          },
          {
            selector: "edge",
            style: {
              "curve-style": "bezier",
              "target-arrow-shape": "triangle",
              "line-color": "#444",
              "target-arrow-color": "#444",
              width: 1,
              "arrow-scale": 0.6,
            },
          },
          {
            selector: "edge[kind = 'area']",
            style: { "line-color": "#664", "target-arrow-color": "#664" },
          },
        ],
        layout: layoutSpec,
        wheelSensitivity: 0.2,
      });
    } catch (err) {
      console.error("cytoscape init failed", err);
      throw err;
    }

    cy.on("tap", "node", (evt) => showNode(evt.target.data()));

    // Hide the loading overlay once layoutstop fires (or immediately for
    // layouts that don't animate).
    const hideLoader = () => {
      const el = document.getElementById("cy-loading");
      if (el) el.style.display = "none";
    };
    cy.one("layoutstop", hideLoader);
    setTimeout(hideLoader, 100); // belt and braces for synchronous layouts

    document.getElementById("stats").textContent =
      graph.nodes.length + " nodes · " + graph.edges.length + " edges";
  }

  function indexEdgesByPage(graph) {
    for (const e of graph.edges) {
      const src = e.data.source;
      if (!edgesByPage.has(src)) edgesByPage.set(src, []);
      edgesByPage.get(src).push(e.data);
    }
  }

  function toNode(n) {
    const d = n.data;
    const data = { ...d, short_label: shortLabel(d) };
    // Only set thumb_url when a thumbnail exists; the style selector
    // [thumb_url] then keys off attribute presence, so unvisited/external
    // nodes never trigger the background-image URL parser.
    if (d.thumb) data.thumb_url = RUN_BASE + d.thumb;
    return { data: data };
  }

  function shortLabel(d) {
    if (d.label && d.label.length > 0 && d.label !== d.url) return truncate(d.label, 30);
    try {
      const u = new URL(d.url);
      return truncate(u.pathname || "/", 30);
    } catch (e) {
      return truncate(d.url, 30);
    }
  }

  function truncate(s, n) {
    return s.length > n ? s.slice(0, n - 1) + "…" : s;
  }

  function showNode(data) {
    document.getElementById("panel-title").textContent = data.label || data.url;

    const openOriginal = document.getElementById("open-page");
    openOriginal.href = data.url;
    openOriginal.style.display = "inline";
    openOriginal.textContent = "open original URL";

    const empty = document.getElementById("empty-msg");
    const iframe = document.getElementById("page-iframe");

    if (!data.archive) {
      empty.style.display = "block";
      iframe.style.display = "none";
      iframe.src = "about:blank";
      let msg;
      if (data.is_external) {
        msg = "external link — not archived. use 'open original URL'.";
      } else if (data.is_phantom_404) {
        msg = "phantom 404 — server returned the homepage for this URL.";
      } else if (data.is_unvisited) {
        msg = "discovered but not crawled (over max-pages cap). re-run with a higher --max-pages to archive.";
      } else {
        msg = "page not archived (capture failed during crawl).";
      }
      empty.textContent = msg;
      return;
    }

    if (data.is_duplicate) {
      // Same body as another URL — show that URL's archive transparently,
      // but tell the user what's going on.
      document.getElementById("panel-title").textContent =
        (data.label || data.url) + "  (alias of already-archived page)";
    }

    empty.style.display = "none";
    iframe.style.display = "block";
    iframe.src = RUN_BASE + data.archive;
    iframe.onload = () => onIframeLoad(data);
  }

  function onIframeLoad(nodeData) {
    if (!highlightsOn) return;
    const iframe = document.getElementById("page-iframe");
    let doc;
    try {
      doc = iframe.contentDocument || iframe.contentWindow.document;
    } catch (e) {
      console.warn("cannot access iframe contents", e);
      return;
    }
    if (!doc) return;
    injectHighlights(doc, nodeData);
  }

  function injectHighlights(doc, nodeData) {
    // Style <a> tags with a visible outline.
    const style = doc.createElement("style");
    style.id = "site-cart-highlights";
    style.textContent =
      "a[href] { outline: 2px solid rgba(255,40,40,0.85) !important;" +
      " outline-offset: 1px; }";
    doc.head && doc.head.appendChild(style);

    // For each <img usemap>, draw poly/rect/circle overlays on a canvas
    // sized to the image, using the area coords from the original HTML
    // (preserved in the MHTML).
    const imgs = doc.querySelectorAll("img[usemap]");
    imgs.forEach((img) => overlayAreas(doc, img));
  }

  function overlayAreas(doc, img) {
    const usemap = (img.getAttribute("usemap") || "").replace(/^#/, "");
    if (!usemap) return;
    const map =
      doc.querySelector('map[name="' + cssEscape(usemap) + '"]') ||
      doc.querySelector('map[id="' + cssEscape(usemap) + '"]');
    if (!map) return;
    const areas = map.querySelectorAll("area[href]");
    if (areas.length === 0) return;

    const draw = () => {
      const w = img.naturalWidth || img.width;
      const h = img.naturalHeight || img.height;
      if (!w || !h) return;
      let canvas = img.parentNode.querySelector("canvas[data-site-cart]");
      if (!canvas) {
        canvas = doc.createElement("canvas");
        canvas.setAttribute("data-site-cart", "1");
        canvas.style.position = "absolute";
        canvas.style.pointerEvents = "none";
        canvas.style.left = img.offsetLeft + "px";
        canvas.style.top = img.offsetTop + "px";
        canvas.style.width = img.clientWidth + "px";
        canvas.style.height = img.clientHeight + "px";
        canvas.width = w;
        canvas.height = h;
        if (img.parentNode.style.position === "" || img.parentNode.style.position === "static") {
          // best-effort: anchor against the body if the parent is not positioned
        }
        img.insertAdjacentElement("afterend", canvas);
      }
      const ctx = canvas.getContext("2d");
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      ctx.strokeStyle = "rgba(255,40,40,0.85)";
      ctx.fillStyle = "rgba(255,40,40,0.18)";
      ctx.lineWidth = Math.max(2, Math.min(w, h) / 400);

      areas.forEach((area) => {
        const shape = (area.getAttribute("shape") || "rect").toLowerCase();
        const coords = (area.getAttribute("coords") || "")
          .split(",")
          .map((s) => parseFloat(s.trim()))
          .filter((n) => !isNaN(n));
        drawShape(ctx, shape, coords);
      });
    };

    if (img.complete && img.naturalWidth) {
      draw();
    } else {
      img.addEventListener("load", draw, { once: true });
    }
  }

  function drawShape(ctx, shape, coords) {
    if (shape === "rect" && coords.length >= 4) {
      const [x1, y1, x2, y2] = coords;
      ctx.beginPath();
      ctx.rect(x1, y1, x2 - x1, y2 - y1);
      ctx.fill();
      ctx.stroke();
    } else if (shape === "circle" && coords.length >= 3) {
      const [cx, cy, r] = coords;
      ctx.beginPath();
      ctx.arc(cx, cy, r, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
    } else if (shape === "poly" && coords.length >= 4) {
      ctx.beginPath();
      ctx.moveTo(coords[0], coords[1]);
      for (let i = 2; i < coords.length; i += 2) {
        ctx.lineTo(coords[i], coords[i + 1]);
      }
      ctx.closePath();
      ctx.fill();
      ctx.stroke();
    }
  }

  function cssEscape(s) {
    if (window.CSS && CSS.escape) return CSS.escape(s);
    return s.replace(/[^a-zA-Z0-9_-]/g, "\\$&");
  }

  document.getElementById("toggle-highlights").addEventListener("change", (e) => {
    highlightsOn = e.target.checked;
    const iframe = document.getElementById("page-iframe");
    if (!iframe.contentDocument) return;
    const doc = iframe.contentDocument;
    const style = doc.getElementById("site-cart-highlights");
    if (style) style.disabled = !highlightsOn;
    const canvases = doc.querySelectorAll("canvas[data-site-cart]");
    canvases.forEach((c) => (c.style.display = highlightsOn ? "block" : "none"));
  });
})();
