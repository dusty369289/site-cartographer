// site-cartographer viewer
// Sigma.js (WebGL) renders the graph; d3-force runs the layout in-browser
// with a tweakable control panel; a separate canvas overlay paints thumbnail
// images on top of the WebGL nodes.

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
    archived: "#6ab0ff",
    canonical: "#5fd97a",
    external: "#d9b35f",
    phantom: "#d97a7a",
    unvisited: "#666666",
  };

  let renderer = null;
  let graph = null;
  let highlightsOn = true;
  let thumbnailsOn = true;
  let edgesByPage = new Map();
  let layoutAbort = false;
  const imageCache = new Map();

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

    edgesByPage = new Map();
    for (const e of data.edges) {
      if (!edgesByPage.has(e.source)) edgesByPage.set(e.source, []);
      edgesByPage.get(e.source).push(e);
    }

    // Seed nodes in a small random circle so d3-force has something to relax.
    const seedR = Math.sqrt(data.nodes.length) * 25;
    for (const n of data.nodes) {
      const angle = Math.random() * Math.PI * 2;
      const r = Math.random() * seedR;
      graph.addNode(n.id, {
        x: Math.cos(angle) * r,
        y: Math.sin(angle) * r,
        size: 4,
        label: shortLabel(n),
        color: pickColor(n),
        _raw: n,
      });
    }
    for (const e of data.edges) {
      if (!graph.hasNode(e.source) || !graph.hasNode(e.target)) continue;
      if (graph.hasEdge(e.source, e.target)) continue;
      graph.addEdge(e.source, e.target, {
        size: 0.5,
        color: e.kind === "area" ? "#403d28" : "#2a2a2a",
        _raw: e,
      });
    }

    // Size by degree (hubs pop).
    let maxDeg = 1;
    graph.forEachNode((n) => { maxDeg = Math.max(maxDeg, graph.degree(n)); });
    graph.forEachNode((n) => {
      const d = graph.degree(n);
      const norm = Math.sqrt(d / maxDeg);
      graph.setNodeAttribute(n, "size", 3 + 14 * norm);
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
      showNode(graph.getNodeAttribute(node, "_raw"));
    });
    renderer.on("enterNode", () => {
      document.getElementById("cy").style.cursor = "pointer";
    });
    renderer.on("leaveNode", () => {
      document.getElementById("cy").style.cursor = "default";
    });
    renderer.on("afterRender", drawThumbnailOverlay);

    document.getElementById("stats").textContent =
      data.nodes.length + " nodes · " + data.edges.length + " edges";

    bindLayoutControls();
    bindHighlightControl();
    setupOverlayCanvas();

    // Kick off the initial layout.
    runLayout(readParams());
  }

  // -------------------------------------------------------------------- layout
  function readParams() {
    return {
      linkDistance: +document.getElementById("param-link-distance").value,
      charge: -(+document.getElementById("param-charge").value),
      collide: +document.getElementById("param-collide").value,
      centerStrength: (+document.getElementById("param-center").value) / 100,
      alphaDecay: (+document.getElementById("param-decay").value) / 1000,
    };
  }

  function bindLayoutControls() {
    const ids = [
      ["param-link-distance", "val-link-distance", (v) => v],
      ["param-charge", "val-charge", (v) => "-" + v],
      ["param-collide", "val-collide", (v) => v],
      ["param-center", "val-center", (v) => (v / 100).toFixed(2)],
      ["param-decay", "val-decay", (v) => (v / 1000).toFixed(3)],
    ];
    for (const [iid, oid, fmt] of ids) {
      const inp = document.getElementById(iid);
      const out = document.getElementById(oid);
      out.textContent = fmt(inp.value);
      inp.addEventListener("input", () => { out.textContent = fmt(inp.value); });
    }
    document.getElementById("layout-run").addEventListener("click", () => {
      runLayout(readParams());
    });
    document.getElementById("layout-stop").addEventListener("click", () => {
      layoutAbort = true;
    });
    document.getElementById("param-thumbnails").addEventListener("change", (e) => {
      thumbnailsOn = e.target.checked;
      drawThumbnailOverlay();
    });
  }

  function runLayout(params) {
    layoutAbort = false;
    const runBtn = document.getElementById("layout-run");
    const stopBtn = document.getElementById("layout-stop");
    const prog = document.getElementById("layout-progress");
    const progBar = prog.querySelector(".bar");
    runBtn.disabled = true;
    stopBtn.disabled = false;
    prog.style.display = "block";
    progBar.style.width = "0%";

    // Build the d3-force simulation off a node array we mutate; sigma reads
    // the same x,y attrs each tick.
    const nodes = [];
    graph.forEachNode((id, attrs) => {
      nodes.push({ id, x: attrs.x, y: attrs.y });
    });
    const links = [];
    graph.forEachEdge((id, attrs, src, tgt) => {
      links.push({ source: src, target: tgt });
    });

    const sim = d3.forceSimulation(nodes)
      .force("link", d3.forceLink(links).id((d) => d.id).distance(params.linkDistance).strength(0.6))
      .force("charge", d3.forceManyBody().strength(params.charge).distanceMax(800))
      .force("center", d3.forceCenter(0, 0).strength(params.centerStrength))
      .force("collide", d3.forceCollide(params.collide))
      .alphaDecay(params.alphaDecay)
      .alpha(1)
      .stop();

    const alphaMin = sim.alphaMin();
    const decay = sim.alphaDecay();
    const totalTicks = Math.max(50, Math.ceil(Math.log(alphaMin) / Math.log(1 - decay)));
    let ticked = 0;

    function step() {
      if (layoutAbort) {
        finish();
        return;
      }
      const batch = 6;
      for (let i = 0; i < batch && sim.alpha() > alphaMin; i++) {
        sim.tick();
        ticked++;
      }
      // Push positions back into sigma graph
      for (const n of nodes) {
        graph.setNodeAttribute(n.id, "x", n.x);
        graph.setNodeAttribute(n.id, "y", n.y);
      }
      progBar.style.width = Math.min(100, (ticked / totalTicks) * 100).toFixed(1) + "%";
      if (sim.alpha() > alphaMin) {
        requestAnimationFrame(step);
      } else {
        finish();
      }
    }

    function finish() {
      runBtn.disabled = false;
      stopBtn.disabled = true;
      progBar.style.width = "100%";
      setTimeout(() => { prog.style.display = "none"; }, 400);
      const loader = document.getElementById("cy-loading");
      if (loader) loader.style.display = "none";
    }

    step();
  }

  // -------------------------------------------------------------- thumbnails
  function setupOverlayCanvas() {
    const cy = document.getElementById("cy");
    const overlay = document.getElementById("thumbnail-overlay");
    const resize = () => {
      const rect = cy.getBoundingClientRect();
      overlay.style.width = rect.width + "px";
      overlay.style.height = rect.height + "px";
      overlay.width = Math.floor(rect.width * (window.devicePixelRatio || 1));
      overlay.height = Math.floor(rect.height * (window.devicePixelRatio || 1));
      drawThumbnailOverlay();
    };
    resize();
    window.addEventListener("resize", resize);
  }

  function loadImage(url) {
    let img = imageCache.get(url);
    if (img) return img;
    img = new Image();
    img.crossOrigin = "anonymous";
    img.onload = () => { if (renderer) renderer.refresh(); };
    img.src = url;
    imageCache.set(url, img);
    return img;
  }

  function drawThumbnailOverlay() {
    if (!renderer || !graph) return;
    const overlay = document.getElementById("thumbnail-overlay");
    if (!overlay) return;
    const ctx = overlay.getContext("2d");
    const dpr = window.devicePixelRatio || 1;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, overlay.width / dpr, overlay.height / dpr);
    if (!thumbnailsOn) return;

    graph.forEachNode((nodeId, attrs) => {
      const raw = attrs._raw;
      if (!raw || !raw.thumb) return;
      const screen = renderer.graphToViewport({ x: attrs.x, y: attrs.y });
      const sizePx = renderer.getNodeDisplayData(nodeId).size;
      // Draw at 1.4× the node radius so the thumb is visible without
      // overwhelming the dot underneath.
      const r = Math.max(8, sizePx * 1.4);
      const img = loadImage(RUN_BASE + raw.thumb);
      if (!img.complete || !img.naturalWidth) return;
      ctx.save();
      ctx.beginPath();
      ctx.arc(screen.x, screen.y, r, 0, Math.PI * 2);
      ctx.clip();
      // cover-fit the image
      const iw = img.naturalWidth;
      const ih = img.naturalHeight;
      const scale = Math.max((2 * r) / iw, (2 * r) / ih);
      const dw = iw * scale;
      const dh = ih * scale;
      ctx.drawImage(img, screen.x - dw / 2, screen.y - dh / 2, dw, dh);
      ctx.restore();
      ctx.beginPath();
      ctx.arc(screen.x, screen.y, r, 0, Math.PI * 2);
      ctx.lineWidth = 1.2;
      ctx.strokeStyle = "rgba(255,255,255,0.25)";
      ctx.stroke();
    });
  }

  // ------------------------------------------------------------- side panel
  function shortLabel(n) {
    if (n.label && n.label !== n.url) return truncate(n.label, 40);
    try {
      const u = new URL(n.url);
      return truncate(u.pathname || "/", 40);
    } catch (e) {
      return truncate(n.url, 40);
    }
  }
  function truncate(s, k) { return s.length > k ? s.slice(0, k - 1) + "…" : s; }

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
      if (data.is_external) msg = "external link — not archived. use 'open original URL'.";
      else if (data.is_phantom_404) msg = "phantom 404 — server returned the homepage for this URL.";
      else if (data.is_unvisited) msg = "discovered but not crawled (over max-pages cap).";
      else msg = "page not archived (capture failed during crawl).";
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
    try { doc = iframe.contentDocument || iframe.contentWindow.document; }
    catch (e) { return; }
    if (!doc) return;
    injectHighlights(doc, nodeData);
  }

  function injectHighlights(doc, nodeData) {
    const style = doc.createElement("style");
    style.id = "site-cart-highlights";
    style.textContent = "a[href] { outline: 2px solid rgba(255,40,40,0.85) !important; outline-offset: 1px; }";
    doc.head && doc.head.appendChild(style);
    doc.querySelectorAll("img[usemap]").forEach((img) => overlayAreas(doc, img));
  }

  function overlayAreas(doc, img) {
    const usemap = (img.getAttribute("usemap") || "").replace(/^#/, "");
    if (!usemap) return;
    const map = doc.querySelector('map[name="' + cssEscape(usemap) + '"]')
      || doc.querySelector('map[id="' + cssEscape(usemap) + '"]');
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
        const coords = (area.getAttribute("coords") || "").split(",").map((s) => parseFloat(s.trim())).filter((n) => !isNaN(n));
        drawShape(ctx, shape, coords);
      });
    };
    if (img.complete && img.naturalWidth) draw();
    else img.addEventListener("load", draw, { once: true });
  }

  function drawShape(ctx, shape, coords) {
    if (shape === "rect" && coords.length >= 4) {
      const [x1, y1, x2, y2] = coords;
      ctx.beginPath(); ctx.rect(x1, y1, x2 - x1, y2 - y1); ctx.fill(); ctx.stroke();
    } else if (shape === "circle" && coords.length >= 3) {
      const [cx, cy, r] = coords;
      ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
    } else if (shape === "poly" && coords.length >= 4) {
      ctx.beginPath(); ctx.moveTo(coords[0], coords[1]);
      for (let i = 2; i < coords.length; i += 2) ctx.lineTo(coords[i], coords[i + 1]);
      ctx.closePath(); ctx.fill(); ctx.stroke();
    }
  }

  function cssEscape(s) { return window.CSS && CSS.escape ? CSS.escape(s) : s.replace(/[^a-zA-Z0-9_-]/g, "\\$&"); }

  function bindHighlightControl() {
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
  }
})();
