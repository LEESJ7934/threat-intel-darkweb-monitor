"use strict";
(() => {
  const dataNode = document.getElementById("dashboard-chart-data");
  if (!dataNode) return;
  let data;
  try { data = JSON.parse(dataNode.textContent); } catch (_) { return; }
  for (const kind of ["risk", "source"]) {
    const target = document.getElementById(kind + "-chart");
    if (!target || !Array.isArray(data[kind])) continue;
    const rows = data[kind];
    const total = rows.reduce((sum, row) => sum + Math.max(0, Number(row.count) || 0), 0);
    if (!total) {
      const empty = document.createElement("p");
      empty.className = "chart-empty";
      empty.textContent = "표시할 사건이 없습니다.";
      target.replaceChildren(empty);
      continue;
    }
    const fragment = document.createDocumentFragment();
    for (const item of rows) {
      const count = Math.max(0, Number(item.count) || 0);
      const row = document.createElement("div");
      row.className = "chart-row";
      const label = document.createElement("span");
      label.className = "chart-label";
      label.textContent = String(item.label);
      const track = document.createElement("div");
      track.className = "chart-track";
      const fill = document.createElement("div");
      fill.className = "chart-fill";
      if (kind === "risk" && ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"].includes(item.label)) {
        fill.classList.add(item.label.toLowerCase());
      }
      fill.style.width = Math.min(100, count / total * 100) + "%";
      track.append(fill);
      const value = document.createElement("span");
      value.className = "chart-count";
      value.textContent = String(count);
      row.append(label, track, value);
      fragment.append(row);
    }
    target.replaceChildren(fragment);
  }
})();
