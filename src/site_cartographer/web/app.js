// Shared utilities for the web UI.

export function fmtBytes(n) {
  if (n == null) return "—";
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  if (n < 1024 ** 3) return (n / (1024 * 1024)).toFixed(1) + " MB";
  return (n / 1024 ** 3).toFixed(1) + " GB";
}

export function escapeHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

export function statusBadge(scan) {
  if (scan.running) return `<span class="badge running">running</span>`;
  if (!scan.finished_at) return `<span class="badge halted">interrupted</span>`;
  if (scan.halt_reason) return `<span class="badge halted">${escapeHtml(shorten(scan.halt_reason, 28))}</span>`;
  return `<span class="badge finished">finished</span>`;
}

export function shorten(s, k) {
  return s == null ? "" : (s.length > k ? s.slice(0, k - 1) + "…" : s);
}

export function fmtTime(ts) {
  if (!ts) return "—";
  return ts.replace("T", " ").slice(0, 16);
}

export async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    ...opts,
  });
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`${path}: HTTP ${res.status} ${body}`);
  }
  if (res.status === 204) return null;
  return res.json();
}
