// site-cartographer viewer (Sigma.js + Graphology)
// Loads graph.json with baked ForceAtlas2 positions, renders via WebGL,
// shows clusters Obsidian-style. Click a node -> side panel loads the saved
// HTML archive in an iframe and highlights clickable elements.

(function () {
  const RUN_BASE = "../";
  const Graph = (window.graphology && window.graphology.Graph)
    || (window.graphology && window.graphology.default && window.graphology.default.Graph);
  if (!Graph) {
    document.getElementById("panel-title").textContent =
      "viewer error: graphology not loaded";
    return;
  }

  const COLORS = {
    archived: "#6ab0ff",       // blue — internal page that was archived
    canonical: "#5fd97a",      // green — archived page that has aliases
    external: "#d9b35f",       // amber — external link
    phantom: "#d97a7a",        // red — phantom 404
    unvisited: "#666666",      // grey — referenced but not crawled
  };

  let renderer = null;
  let graph = null;
  let highlightsOn = true;
  let edgesByPage = new Map();

  fetch(RUN_BASE + "graph.json")
    .then((r) => {
      if (!r.ok) throw new Error("graph.json: HTTP " + r.status);
      return r.json();
    })
    .then(initGraph)
    .catch((err) => {
      document.getElementById("panel-title").textContent =
        "viewer error: " + err.message;
      console.error(err);
    });

  function pickColor(d) {
    if (d.is_phantom_404) return COLORS.phantom;
    if (d.is_external) return COLORS.external;
    if (d.is_unvisited) return COLORS.unvisited;
    if (d.alias_count) return COLORS.canonical;
    if (d.archive) return COLORS.archived;
    return COLORS.unvisited;
  }

  function initGraph(data) {
    graph = new Graph({ multi: false, type: "undirected" });

    // Index edges by source for the side-panel highlight metadata.
    edgesByPage = new Map();
    for (const e of data.edges) {
      if (!edgesByPage.has(e.source)) edgesByPage.set(e.source, []);
      edgesByPage.get(e.source).push(e);
    }

    for (const n of data.nodes) {
      graph.addNode(n.id, {
        x: n.x ?? 0,
        y: n.y ?? 0,
        size: 4,
        label: shortLabel(n),
        color: pickColor(n),
        // stash the original record for click handlers / panel
        _raw: n,
      });
    }
    for (const e of data.edges) {
      if (graph.hasEdge(e.source, e.target)) continue;
      if (!graph.hasNode(e.source) || !graph.hasNode(e.target)) continue;
      graph.addEdge(e.source, e.target, {
        size: 0.5,
        color: e.kind === "area" ? "#404030" : "#2a2a2a",
        _raw: e,
      });
    }

    // Size nodes by degree so hubs pop visually.
    let maxDeg = 1;
    graph.forEachNode((n) => { maxDeg = Math.max(maxDeg, graph.degree(n)); });
    graph.forEachNode((n) => {
      const d = graph.degree(n);
      const norm = Math.sqrt(d / maxDeg);
      graph.setNodeAttribute(n, "size", 3 + 12 * norm);
    });

    renderer = new Sigma(graph, document.getElementById("cy"), {
      renderLabels: true,
      labelDensity: 0.07,
      labelGridCellSize: 60,
      labelRenderedSizeThreshold: 8,
      defaultEdgeColor: "#2a2a2a",
      minCameraRatio: 0.05,
      maxCameraRatio: 20,
      labelColor: { color: "#ddd" },
      labelFont: "system-ui, sans-serif",
      labelSize: 11,
    });

    renderer.on("clickNode", ({ node }) => {
      const raw = graph.getNodeAttribute(node, "_raw");
      showNode(raw);
    });

    renderer.on("enterNode", ({ node }) => {
      document.getElementById("cy").style.cursor = "pointer";
    });
    renderer.on("leaveNode", () => {
      document.getElementById("cy").style.cursor = "default";
    });

    document.getElementById("cy-loading").style.display = "none";
    document.getElementById("stats").textContent =
      data.nodes.length + " nodes · " + data.edges.length + " edges";
  }

  function shortLabel(n) {
    if (n.label && n.label !== n.url) return truncate(n.label, 40);
    try {
      const u = new URL(n.url);
      return truncate(u.pathname || "/", 40);
    } catch (e) {
      return truncate(n.url, 40);
    }
  }

  function truncate(s, k) {
    return s.length > k ? s.slice(0, k - 1) + "…" : s;
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

    if (data.alias_count) {
      const aliasNote = ` · ${data.alias_count} alias` +
        (data.alias_count === 1 ? "" : "es") + " (other URLs serving this content)";
      document.getElementById("panel-title").textContent =
        (data.label || data.url) + aliasNote;
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
    const style = doc.createElement("style");
    style.id = "site-cart-highlights";
    style.textContent =
      "a[href] { outline: 2px solid rgba(255,40,40,0.85) !important;" +
      " outline-offset: 1px; }";
    doc.head && doc.head.appendChild(style);

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
