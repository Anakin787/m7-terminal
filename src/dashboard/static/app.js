/* M7 Terminal dashboard.
 *
 * Charts are hand-rolled SVG rather than a charting library: the page must be
 * self-contained, and the two forms needed here (one area line, one donut) are
 * small enough that a dependency would cost more than it saves.
 *
 * Colors come from the project design system (docs/ui/DESIGN.md). The
 * categorical pair used by the donut was validated for colour-vision
 * deficiency separation before use, and every segment is direct-labelled in
 * the legend so identity never rests on colour alone.
 */

const COLORS = {
  primary: "#2563eb",       // primary-container - series line
  positive: "#4edea3",      // secondary-fixed-dim
  negative: "#ffb2b7",      // tertiary-fixed-dim
  warning: "#f59e0b",
  ink: "#dae2fd",
  inkMuted: "#c3c6d7",
};

// Fixed assignment: a bucket keeps its colour regardless of ordering or count.
const ALLOCATION_COLORS = {
  KR: "#00a572", US: "#2563eb", KRW: "#00a572", USD: "#2563eb", OTHER: "#8d90a0",
  // Buckets read as a risk ladder: calm, core, speculative - then grey for
  // the holdings no plan covers, which must not look like a fourth bucket.
  SAFE: "#0891b2", CORE: "#2563eb", GROWTH: "#c026d3", UNMANAGED: "#8d90a0",
};

const state = { view: "overview", range: "3M", allocBy: "market", history: null,
                editingName: false, auditCategory: "", trading: null, health: null };

/* ---------------------------------------------------------------- helpers */

const $ = (id) => document.getElementById(id);

function fmtInt(value) {
  if (value === null || value === undefined) return "—";
  return Math.round(value).toLocaleString("en-US");
}

function fmtSigned(value) {
  if (value === null || value === undefined) return "—";
  const rounded = Math.round(value);
  return (rounded >= 0 ? "+" : "") + rounded.toLocaleString("en-US");
}

function fmtPct(rate) {
  if (rate === null || rate === undefined) return "—";
  return (rate >= 0 ? "+" : "") + (rate * 100).toFixed(2) + "%";
}

function fmtPrice(value, currency) {
  if (value === null || value === undefined) return "—";
  const digits = currency === "KRW" ? 0 : 2;
  return value.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

function toneClass(value) {
  if (value === null || value === undefined || value === 0) return "text-on-surface";
  return value > 0 ? "text-secondary-fixed-dim" : "text-tertiary-fixed-dim";
}

/** Markdown the AI wrote, read as the prose it is.
 *
 * Gemini answers in Markdown and Notion renders it, so the stored comment
 * keeps its markers - that copy is the one being delivered. Here they are
 * only noise: these two places show a flattened excerpt, so `**` never
 * becomes bold, it just spends characters and reads as typing debris.
 *
 * Stripped rather than rendered, deliberately. A one-line card and a
 * two-line clamp have nowhere to put a heading or a nested list, and
 * pulling in a Markdown renderer to throw its output away is a lot of
 * machinery for a preview. Emphasis with a single `*` is left alone: it
 * does not appear in these reports, and the rule that would catch it also
 * catches arithmetic.
 */
function plainText(value) {
  return (value || "")
    .replace(/```[\s\S]*?```/g, " ")                  // fenced code
    .replace(/^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/gm, " ") // horizontal rules
    .replace(/^\s*#{1,6}\s+/gm, "")                   // headings
    .replace(/^\s*[*+-]\s+/gm, "· ")                  // bullets, kept as a mark
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")          // links
    .replace(/(\*\*|__)([\s\S]*?)\1/g, "$2")          // bold
    .replace(/`([^`]*)`/g, "$1")                      // inline code
    .replace(/\s+/g, " ")
    .trim();
}

async function getJSON(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return response.json();
}

function showError(detail) {
  $("error-detail").textContent = detail || "";
  $("error-banner").hidden = !detail;
}

/* ------------------------------------------------------------ navigation */

// The hash is the address of the page. Clicking a nav item used to change
// only the in-memory view, which meant a refresh - or a copied link - had no
// hash to read and always landed on Overview, whatever you were looking at.
function knownView(view) {
  return document.querySelector(`.nav-item[data-view="${view}"]`) ? view : "overview";
}

function currentHashView() {
  return knownView(decodeURIComponent(location.hash.slice(1)) || "overview");
}

function setView(requested) {
  const view = knownView(requested);
  state.view = view;
  const raw = decodeURIComponent(location.hash.slice(1));
  if (raw !== view) {
    // Moving between pages pushes a history entry so Back works. Correcting
    // an empty or unrecognised hash rewrites in place instead - Back should
    // not walk into an address that never named a page.
    // Either way the write re-enters through hashchange, where the view
    // already matches and the second pass stops here.
    if (raw && knownView(raw) === raw) location.hash = view;
    else if (window.history?.replaceState) history.replaceState(null, "", "#" + view);
    else location.hash = view;
  }
  document.querySelectorAll("section[data-view]").forEach((section) => {
    section.hidden = section.dataset.view !== view;
  });
  document.querySelectorAll(".nav-item").forEach((item) => {
    const active = item.dataset.view === view;
    item.className =
      "nav-item flex items-center gap-3 px-3 py-2.5 rounded transition-colors " +
      (active
        ? "text-primary font-bold border-r-2 border-primary bg-surface-container-high"
        : "text-on-surface-variant hover:bg-surface-container-high");
  });
  // Read the title off the nav item rather than keeping a second copy here.
  // Two lists of the same names drift, and the header showing "audit" while
  // the nav and the section heading both said "Audit Log" is what that drift
  // looks like.
  const navItem = document.querySelector(`.nav-item[data-view="${view}"]`);
  $("page-title").textContent = (navItem && navItem.dataset.title) || view;

  if (view === "holdings") loadHoldings();
  if (view === "reports") loadReports();
  if (view === "audit") loadAudit().catch((err) => showError(String(err)));
  if (view === "settings") loadSettings();
}

/* -------------------------------------------------------------- overview */

async function loadOverview() {
  let data;
  try {
    data = await getJSON("/api/overview");
  } catch (err) {
    showError(String(err));
    return;
  }
  if (!data.ready) { showError(data.error); return; }
  showError(data.error);

  $("kpi-total").textContent = fmtInt(data.total_krw);
  $("kpi-total-usd").textContent = data.total_usd_equivalent
    ? "≈ $" + fmtInt(data.total_usd_equivalent) : "—";
  // Invested capital, converted at the purchase-time rate where one is known.
  $("kpi-invested").textContent = fmtInt(data.purchase_krw) + " KRW";

  $("kpi-pnl").textContent = fmtSigned(data.profit_krw);
  $("kpi-pnl-wrap").className = "font-data-mono text-2xl font-bold tracking-tight " + toneClass(data.profit_krw);

  // The API reports both nominal and after-fee returns; show the real one
  // when Toss provides it and say which is on screen.
  const hasAfterCost = data.profit_rate_after_cost !== null && data.profit_rate_after_cost !== undefined;
  const shownRate = hasAfterCost ? data.profit_rate_after_cost : data.profit_rate;
  const badge = $("kpi-pnl-rate");
  badge.textContent = fmtPct(shownRate);
  badge.className = "px-1.5 py-0.5 rounded font-data-mono text-[10px] font-bold " +
    (shownRate >= 0 ? "bg-secondary-container/20 text-secondary-fixed-dim" : "bg-tertiary-container/20 text-tertiary-fixed-dim");
  $("kpi-pnl-note").textContent = hasAfterCost
    ? "after fees & tax" : (data.has_unconverted_fx ? "환차손익 미반영" : "nominal");

  // Only foreign positions with a known purchase-time rate contribute; when
  // none do, hide the row instead of showing a misleading "0".
  const hasFx = data.fx_pnl_krw !== null && data.fx_pnl_krw !== undefined;
  $("kpi-fx-row").hidden = !hasFx;
  if (hasFx) {
    $("kpi-fx").textContent = fmtSigned(data.fx_pnl_krw) + " KRW";
    $("kpi-fx").className = toneClass(data.fx_pnl_krw);
  }

  $("kpi-day").textContent = fmtSigned(data.daily_profit_krw);
  $("kpi-day-wrap").className = "font-data-mono text-2xl font-bold tracking-tight " + toneClass(data.daily_profit_krw);
  $("kpi-day-rate").textContent = fmtPct(data.daily_profit_rate);
  $("kpi-day-arrow").textContent = data.daily_profit_krw > 0 ? "arrow_upward"
    : data.daily_profit_krw < 0 ? "arrow_downward" : "remove";
  $("kpi-day-rate-wrap").className =
    "flex items-center gap-1 mt-1 font-data-mono text-[11px] " + toneClass(data.daily_profit_krw);

  const power = data.buying_power || {};
  $("kpi-cash").textContent = fmtInt(power.KRW);
  $("kpi-cash-usd").textContent = power.USD !== null && power.USD !== undefined
    ? "USD " + fmtInt(power.USD) : "—";

  $("fx-chip").textContent = "USD/KRW: " + (data.exchange_rate
    ? data.exchange_rate.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : "—");

  renderMarketChip("krx-chip", "KRX", data.market_status?.KR);
  renderMarketChip("us-chip", "US", data.market_status?.US);
  renderAlerts(data.warnings || []);
}

// Extended-hours sessions are tradable but are not the main session, so they
// get their own muted style rather than the full "Open" green.
const SESSION_LABELS = { regular: "Open", day: "Day", pre: "Pre", after: "After" };

function renderMarketChip(id, label, status) {
  const chip = $(id);
  const dim = "text-on-surface-variant/50 border border-outline-variant/30 px-2 py-1 rounded";
  if (!status || !status.known) {
    chip.textContent = `${label}: —`;
    chip.className = dim;
    return;
  }
  const text = SESSION_LABELS[status.session];
  if (!text) {
    chip.textContent = `${label}: Closed`;
    chip.className = dim;
    return;
  }
  const tone = status.session === "regular"
    ? "text-secondary-fixed-dim bg-secondary-container/10 border-secondary-container/30"
    : "text-tertiary-fixed-dim bg-tertiary-container/10 border-tertiary-container/30";
  chip.innerHTML = `<span class="w-1.5 h-1.5 rounded-full bg-current inline-block"></span> ${label}: ${text}`;
  chip.className = `${tone} border px-2 py-1 rounded flex items-center gap-1`;
}

// Alerts that are about the system rather than about a holding: the engine
// being halted, and the daily job having stopped running. The second one is
// here because an outage in August lasted eight days without appearing
// anywhere - a report that does not run produces no error to show.
function systemAlerts() {
  const alerts = [];
  const kill = state.trading?.kill_switch;
  if (kill?.active) {
    alerts.push({
      color: COLORS.negative,
      icon: "block",
      title: "킬 스위치 발동 — 자동매매가 중단되어 있습니다",
      detail: (kill.reason ? `사유: ${kill.reason} · ` : "") +
        (kill.engaged_at ? `${fmtStamp(kill.engaged_at)}부터` : "") +
        (kill.engaged_at_source === "mtime" ? " (파일 수정시각 기준)" : ""),
    });
  }
  const health = state.health;
  if (health?.snapshot_stale) {
    const hours = Math.round(health.snapshot_age_hours);
    alerts.push({
      color: COLORS.warning,
      icon: "update_disabled",
      title: `일일 스냅샷이 ${hours}시간째 갱신되지 않았습니다`,
      detail: `마지막 기록 ${fmtStamp(health.last_snapshot_ts)}. 예약 실행이 실패하고 있을 수 있습니다 ` +
        "— 스냅샷은 소급 생성할 수 없으므로 놓친 날은 영구히 빕니다. logs\\report_*.log와 작업 스케줄러를 확인하세요.",
    });
  }
  return alerts;
}

function renderAlert({ color, icon, title, detail }) {
  const div = document.createElement("div");
  div.className = "bg-surface-container-high border-l-[3px] rounded shadow-sm p-4 flex items-start gap-3 mb-gutter";
  div.style.borderLeftColor = color;
  div.innerHTML = `
    <span class="material-symbols-outlined mt-0.5" style="color:${color};font-variation-settings:'FILL' 1;">${icon}</span>
    <div><p class="text-[15px] font-semibold text-on-surface leading-tight"></p>
    <p class="text-body-md text-on-surface-variant mt-1 text-sm"></p></div>`;
  const [titleEl, detailEl] = div.querySelectorAll("p");
  titleEl.textContent = title;
  detailEl.textContent = detail;
  return div;
}

function renderAlerts(warnings) {
  const host = $("alerts");
  host.innerHTML = "";
  systemAlerts().forEach((alert) => host.appendChild(renderAlert(alert)));
  warnings.forEach((warning) => {
    const div = document.createElement("div");
    div.className = "bg-surface-container-high border-l-[3px] rounded shadow-sm p-4 flex items-start gap-3 mb-gutter";
    div.style.borderLeftColor = COLORS.warning;
    div.innerHTML = `
      <span class="material-symbols-outlined mt-0.5" style="color:${COLORS.warning};font-variation-settings:'FILL' 1;">warning</span>
      <div><p class="text-[15px] font-semibold text-on-surface leading-tight"></p>
      <p class="text-body-md text-on-surface-variant mt-1 text-sm">토스 API가 보고한 매수 유의사항입니다. 해당 종목의 거래가 제한되거나 변동성이 확대된 상태일 수 있습니다.</p></div>`;
    div.querySelector("p").textContent = warning;
    host.appendChild(div);
  });
}

/* ----------------------------------------------------------------- chart */

async function loadHistory() {
  const data = await getJSON(`/api/history?range=${state.range}`);
  state.history = data;
  renderChart(data);
}

function renderChart(data) {
  const svg = $("chart");
  const points = data.points || [];
  const empty = $("chart-empty");

  // Two points do not make a trend line. Say what is happening instead of
  // drawing something that implies more history than exists.
  if (points.length < 3) {
    svg.innerHTML = "";
    empty.hidden = false;
    $("chart-empty-detail").textContent =
      `스냅샷 ${data.total_snapshots}개 수집됨 · main.py 를 실행할 때마다 한 점씩 쌓입니다.`;
    return;
  }
  empty.hidden = true;

  const host = $("chart-host");
  const W = host.clientWidth || 800;
  const H = host.clientHeight || 320;
  const pad = { top: 16, right: 16, bottom: 28, left: 68 };
  const innerW = Math.max(1, W - pad.left - pad.right);
  const innerH = Math.max(1, H - pad.top - pad.bottom);

  const values = points.map((p) => p.total_krw);
  let min = Math.min(...values);
  let max = Math.max(...values);
  if (min === max) { min -= 1; max += 1; }
  const span = max - min;
  min -= span * 0.1;
  max += span * 0.1;

  const x = (i) => pad.left + (points.length === 1 ? innerW / 2 : (i / (points.length - 1)) * innerW);
  const y = (v) => pad.top + innerH - ((v - min) / (max - min)) * innerH;

  const line = points.map((p, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(p.total_krw).toFixed(1)}`).join(" ");
  const area = `${line} L${x(points.length - 1).toFixed(1)},${pad.top + innerH} L${x(0).toFixed(1)},${pad.top + innerH} Z`;

  // Four recessive gridlines with value labels; axis text uses ink tokens,
  // never the series colour.
  let grid = "";
  for (let i = 0; i <= 4; i++) {
    const value = min + ((max - min) * i) / 4;
    const gy = y(value);
    grid += `<line x1="${pad.left}" y1="${gy}" x2="${W - pad.right}" y2="${gy}" stroke="${COLORS.ink}" stroke-opacity="0.08"/>`;
    grid += `<text x="${pad.left - 8}" y="${gy + 4}" text-anchor="end" font-size="10" font-family="JetBrains Mono, monospace" fill="${COLORS.inkMuted}" fill-opacity="0.6">${shortKRW(value)}</text>`;
  }

  const firstLabel = points[0].ts.slice(0, 10);
  const lastLabel = points[points.length - 1].ts.slice(0, 10);

  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.innerHTML = `
    <defs><linearGradient id="areaFill" x1="0" x2="0" y1="0" y2="1">
      <stop offset="0%" stop-color="${COLORS.primary}" stop-opacity="0.4"/>
      <stop offset="100%" stop-color="${COLORS.primary}" stop-opacity="0"/>
    </linearGradient></defs>
    ${grid}
    <path d="${area}" fill="url(#areaFill)"/>
    <path d="${line}" fill="none" stroke="${COLORS.primary}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
    <text x="${pad.left}" y="${H - 8}" font-size="10" font-family="JetBrains Mono, monospace" fill="${COLORS.inkMuted}" fill-opacity="0.6">${firstLabel}</text>
    <text x="${W - pad.right}" y="${H - 8}" text-anchor="end" font-size="10" font-family="JetBrains Mono, monospace" fill="${COLORS.inkMuted}" fill-opacity="0.6">${lastLabel}</text>
    <line id="crosshair" y1="${pad.top}" y2="${pad.top + innerH}" stroke="${COLORS.ink}" stroke-opacity="0.3" stroke-dasharray="3 3" style="display:none"/>
    <circle id="cursor-dot" r="4.5" fill="${COLORS.primary}" stroke="#171f33" stroke-width="2" style="display:none"/>
    <rect id="chart-hit" x="${pad.left}" y="${pad.top}" width="${innerW}" height="${innerH}" fill="transparent"/>`;

  attachHover(svg, points, x, y, pad, innerH);
}

function attachHover(svg, points, x, y, pad, innerH) {
  const hit = svg.querySelector("#chart-hit");
  const crosshair = svg.querySelector("#crosshair");
  const dot = svg.querySelector("#cursor-dot");
  const tooltip = $("tooltip");
  const host = $("chart-host");

  hit.addEventListener("mousemove", (event) => {
    const box = svg.getBoundingClientRect();
    const scale = svg.viewBox.baseVal.width / box.width;
    const px = (event.clientX - box.left) * scale;

    let nearest = 0;
    let best = Infinity;
    points.forEach((_, i) => {
      const distance = Math.abs(x(i) - px);
      if (distance < best) { best = distance; nearest = i; }
    });

    const point = points[nearest];
    const cx = x(nearest);
    const cy = y(point.total_krw);

    crosshair.setAttribute("x1", cx); crosshair.setAttribute("x2", cx);
    crosshair.style.display = "";
    dot.setAttribute("cx", cx); dot.setAttribute("cy", cy);
    dot.style.display = "";

    tooltip.innerHTML =
      `<div class="text-on-surface-variant/70 mb-1">${point.ts.replace("T", " ").slice(0, 16)}</div>` +
      `<div class="text-on-surface font-bold">${fmtInt(point.total_krw)} KRW</div>` +
      `<div class="${point.profit_rate >= 0 ? "text-secondary-fixed-dim" : "text-tertiary-fixed-dim"}">${fmtPct(point.profit_rate)}</div>`;
    tooltip.hidden = false;

    const left = (cx / scale) + 14;
    const maxLeft = host.clientWidth - tooltip.offsetWidth - 8;
    tooltip.style.left = Math.min(left, maxLeft) + "px";
    tooltip.style.top = Math.max(8, (cy / scale) - 20) + "px";
  });

  hit.addEventListener("mouseleave", () => {
    crosshair.style.display = "none";
    dot.style.display = "none";
    tooltip.hidden = true;
  });
}

function shortKRW(value) {
  const abs = Math.abs(value);
  if (abs >= 1e8) return (value / 1e8).toFixed(1) + "억";
  if (abs >= 1e4) return (value / 1e4).toFixed(0) + "만";
  return Math.round(value).toLocaleString("en-US");
}

/* ------------------------------------------------------------ allocation */

async function loadAllocation() {
  const data = await getJSON(`/api/allocation?by=${state.allocBy}`);
  // A bucket at zero is exactly the case worth seeing, but a zero-length arc
  // draws nothing - so the donut skips them while the legend keeps them.
  const segments = data.segments || [];
  renderDonut(segments.filter((s) => s.share > 0), segments);
}

function renderDonut(segments, legendSegments) {
  const svg = $("donut");
  const legend = $("alloc-legend");
  legend.innerHTML = "";
  const rows = legendSegments || segments;

  if (!rows.length) {
    svg.innerHTML = `<circle cx="50" cy="50" r="40" fill="none" stroke="#2d3449" stroke-width="12"/>`;
    $("donut-label").textContent = "—";
    $("donut-value").textContent = "—";
    return;
  }

  const R = 40;
  const CIRC = 2 * Math.PI * R;
  let offset = 0;
  let markup = `<circle cx="50" cy="50" r="${R}" fill="none" stroke="#2d3449" stroke-width="12"/>`;

  segments.forEach((segment) => {
    const color = ALLOCATION_COLORS[segment.key] || ALLOCATION_COLORS.OTHER;
    const length = segment.share * CIRC;
    // A 2px surface gap keeps adjacent segments from reading as one arc.
    const gap = segments.length > 1 ? 2 : 0;
    markup += `<circle cx="50" cy="50" r="${R}" fill="none" stroke="${color}" stroke-width="12"
      stroke-dasharray="${Math.max(0, length - gap).toFixed(2)} ${(CIRC - length + gap).toFixed(2)}"
      stroke-dashoffset="${(-offset).toFixed(2)}"><title>${segment.label}: ${(segment.share * 100).toFixed(1)}%</title></circle>`;
    offset += length;
  });

  // The legend walks every row, including the ones the donut could not draw.
  // A bucket sitting at zero against a 20% target is the single most useful
  // thing this chart can say, and it has no arc to say it with.
  rows.forEach((segment) => {
    const color = ALLOCATION_COLORS[segment.key] || ALLOCATION_COLORS.OTHER;
    const row = document.createElement("div");
    row.className = "flex justify-between items-center text-sm";
    row.innerHTML =
      `<div class="flex items-center gap-2"><div class="swatch w-3 h-3 rounded-sm shrink-0"></div><span class="label text-on-surface"></span></div>` +
      `<span class="value font-data-mono font-medium text-on-surface-variant"></span>`;
    row.querySelector(".swatch").style.backgroundColor = color;
    row.querySelector(".label").textContent = segment.label;
    // With a plan to compare against, the share and the gap say more than the
    // won figure: "off plan" should be readable without doing the subtraction.
    if (segment.target != null) {
      const gap = segment.share - segment.target;
      const arrow = gap > 0.0005 ? "\u25b2" : gap < -0.0005 ? "\u25bc" : "=";
      row.querySelector(".value").textContent =
        `${(segment.share * 100).toFixed(1)}% / ${(segment.target * 100).toFixed(0)}% ` +
        `${arrow}${(Math.abs(gap) * 100).toFixed(1)}`;
    } else {
      if (segment.unmanaged) {
        row.querySelector(".label").textContent = `${segment.label} (\uacc4\ud68d \ubc16)`;
      }
      row.querySelector(".value").textContent = fmtInt(segment.value_krw);
    }
    legend.appendChild(row);
  });

  svg.innerHTML = markup;
  const lead = segments[0] || rows[0];
  $("donut-label").textContent = lead.label || lead.key;
  $("donut-value").textContent = (lead.share * 100).toFixed(0) + "%";
}

/* -------------------------------------------------------------- holdings */

async function loadHoldings() {
  if (state.editingName) return;   // never rebuild the table mid-edit
  const data = await getJSON("/api/holdings");
  const body = $("holdings-body");
  body.innerHTML = "";
  const positions = data.positions || [];
  $("holdings-count").textContent = `${positions.length} positions`;

  if (!positions.length) {
    body.innerHTML = `<tr><td colspan="12" class="px-4 py-8 text-center text-on-surface-variant font-body-md">
      보유 종목이 없습니다. 토스 계좌 보유분은 자동으로, 타 증권사 보유분은 config.yaml 의 portfolio.manual 로 표시됩니다.</td></tr>`;
    return;
  }

  positions.forEach((position) => {
    const row = document.createElement("tr");
    row.className = "border-b border-outline-variant/20 hover:bg-surface-container-high transition-colors h-8";
    const isToss = position.source.includes("toss");
    const badgeClass = isToss
      ? "bg-primary-container/20 text-primary border-primary/30"
      : "bg-surface-container-highest text-on-surface-variant border-outline-variant/50";
    row.innerHTML = `
      <td class="px-4 py-1.5 text-on-surface">${position.symbol || "—"}</td>
      <td class="name-cell px-4 py-1.5 font-body-md"></td>
      <td class="px-4 py-1.5"><span class="text-[10px] px-1.5 py-0.5 rounded border ${badgeClass}">${isToss ? "토스" : "수기"}</span></td>
      <td class="px-4 py-1.5 text-right">${position.quantity}</td>
      <td class="px-4 py-1.5 text-right">${fmtPrice(position.last_price, position.currency)} <span class="text-on-surface-variant/50 text-[10px]">${position.currency}</span></td>
      <td class="px-4 py-1.5 text-right text-on-surface-variant">${fmtPrice(position.avg_price, position.currency)}</td>
      <td class="px-4 py-1.5 text-right text-on-surface-variant">${fmtInt(position.cost_krw)}</td>
      <td class="px-4 py-1.5 text-right">${fmtInt(position.value_krw)}</td>
      <td class="px-4 py-1.5 text-right ${toneClass(position.profit_krw)}">${fmtSigned(position.profit_krw)}</td>
      <td class="px-4 py-1.5 text-right ${toneClass(position.profit_rate)}">${fmtPct(position.profit_rate)}</td>
      <td class="px-4 py-1.5 text-right ${toneClass(position.fx_pnl_krw)}">${fmtSigned(position.fx_pnl_krw)}</td>
      <td class="px-4 py-1.5 text-right text-on-surface-variant">${(position.weight * 100).toFixed(1)}%</td>`;
    makeNameEditable(row.querySelector(".name-cell"), position);
    body.appendChild(row);
  });
}

/* Toss reports name == symbol for some tickers (IONX, TSLL), so the name is
 * click-to-edit. The override is stored server-side and also applies to the
 * Notion report, so a ticker reads the same in both places. */
function makeNameEditable(cell, position) {
  if (!position.symbol) {           // static assets are named in config.yaml
    cell.textContent = position.name;
    cell.className += " text-on-surface-variant";
    return;
  }

  const render = (name) => {
    cell.textContent = name;
    cell.title = "클릭해서 이름 수정";
    cell.className =
      "name-cell px-4 py-1.5 font-body-md text-on-surface-variant cursor-text " +
      "hover:text-on-surface hover:underline decoration-dotted underline-offset-4";
  };

  const edit = () => {
    state.editingName = true;
    const current = cell.textContent;
    const input = document.createElement("input");
    input.value = current;
    input.maxLength = 80;
    input.className =
      "w-full bg-surface-container-lowest border border-primary/60 rounded px-2 py-0.5 " +
      "text-on-surface font-body-md outline-none";
    cell.textContent = "";
    cell.className = "name-cell px-2 py-1";
    cell.appendChild(input);
    input.focus();
    input.select();

    let done = false;
    const finish = async (save) => {
      if (done) return;
      done = true;
      state.editingName = false;
      const value = input.value.trim();
      if (!save || value === current) { render(current); return; }
      render(value || position.symbol);
      try {
        const res = await fetch(`/api/holdings/${encodeURIComponent(position.symbol)}/name`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: value }),
        });
        if (!res.ok) throw new Error(`${res.status}`);
        const data = await res.json();
        render(data.name || position.symbol);   // blank clears the override
      } catch (err) {
        render(current);                        // put the old name back
        showError(`이름 저장 실패: ${err}`);
      }
    };

    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") finish(true);
      if (event.key === "Escape") finish(false);
    });
    input.addEventListener("blur", () => finish(true));
  };

  render(position.name);
  cell.addEventListener("click", () => { if (!cell.querySelector("input")) edit(); });
}

/* --------------------------------------------------------------- reports */

async function loadReports() {
  const data = await getJSON("/api/reports");
  const body = $("reports-body");
  body.innerHTML = "";
  const reports = data.reports || [];

  if (!reports.length) {
    body.innerHTML = `<tr><td colspan="4" class="px-4 py-8 text-center text-on-surface-variant">
      아직 생성된 리포트가 없습니다. <code class="font-data-mono text-primary">python main.py</code> 를 실행하세요.</td></tr>`;
  } else {
    reports.forEach((report) => {
      const row = document.createElement("tr");
      row.className = "border-b border-outline-variant/20 hover:bg-surface-container-high transition-colors align-top";

      const whenCell = document.createElement("td");
      whenCell.className = "px-4 py-3 font-data-mono text-xs text-on-surface-variant whitespace-nowrap";
      whenCell.textContent = report.ts || "—";

      const titleCell = document.createElement("td");
      titleCell.className = "px-4 py-3 font-semibold text-on-surface";
      titleCell.textContent = report.title || "Report";

      const commentCell = document.createElement("td");
      commentCell.className = "px-4 py-3 text-sm text-on-surface-variant";
      const comment = document.createElement("p");
      comment.className = "line-clamp-2 max-w-2xl";
      comment.textContent = plainText(report.ai_comment).slice(0, 220);
      commentCell.appendChild(comment);

      const linkCell = document.createElement("td");
      linkCell.className = "px-4 py-3 text-right whitespace-nowrap";
      if (report.url) {
        const open = document.createElement("a");
        open.className = "inline-block border border-outline-variant hover:bg-surface-container " +
          "px-3 py-1.5 rounded text-label-caps font-bold tracking-wide";
        open.target = "_blank";
        open.rel = "noopener";
        open.href = report.url;
        open.textContent = "OPEN";
        linkCell.appendChild(open);
      } else {
        linkCell.className += " text-on-surface-variant/40";
        linkCell.textContent = "—";
      }

      row.append(whenCell, titleCell, commentCell, linkCell);
      body.appendChild(row);
    });
  }

  // Surface the newest AI comment on the Overview card too. This runs even
  // when the list is empty above - the card is driven by the same fetch.
  const latest = reports[0];
  if (latest && latest.ai_comment) {
    // Strip before truncating, or the character budget goes on markers the
    // reader cannot see - and a cut landing inside `**` leaves it dangling.
    const insight = plainText(latest.ai_comment);
    $("ai-text").textContent =
      insight.length > 260 ? insight.slice(0, 260) + "…" : insight;
    $("ai-ts").textContent = latest.ts;
    if (latest.url) $("ai-link").href = latest.url; else $("ai-link").hidden = true;
    $("ai-card").hidden = false;
  }
}

/* -------------------------------------------------------------- settings */

async function loadSettings() {
  const data = await getJSON("/api/settings");
  $("settings-body").textContent = JSON.stringify(data, null, 2);
}

/* ----------------------------------------------------------------- health */

async function loadHealth() {
  try {
    const data = await getJSON("/api/health");
    state.health = data;
    $("api-status").textContent = "Brokerage API: " + (data.connected ? "Connected" : "Error");
    $("api-dot").className = "w-2 h-2 rounded-full " +
      (data.connected ? "bg-secondary-container animate-pulse" : "bg-error");
    $("last-sync").textContent = "Last Sync: " +
      (data.last_sync ? new Date(data.last_sync).toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit" }) : "—");
    renderSnapshotAge(data);
  } catch (err) {
    $("api-status").textContent = "Brokerage API: Offline";
    $("api-dot").className = "w-2 h-2 rounded-full bg-error";
  }
}

// "Last Sync" is when this page last talked to the brokerage, which says
// nothing about whether the daily job ran. They are different clocks and the
// footer now shows both, because only the second one ever went quiet.
function renderSnapshotAge(health) {
  const line = $("snapshot-age");
  if (!line) return;
  if (health.last_snapshot_ts === null || health.last_snapshot_ts === undefined) {
    line.textContent = "Snapshot: —";
    line.className = "text-on-surface-variant/70";
    return;
  }
  const hours = health.snapshot_age_hours;
  line.textContent = "Snapshot: " + (hours < 1 ? "방금" : `${Math.round(hours)}h ago`);
  line.className = health.snapshot_stale ? "text-tertiary-fixed-dim font-bold" : "text-on-surface-variant/70";
  line.title = `마지막 스냅샷 ${fmtStamp(health.last_snapshot_ts)} · ` +
    `${health.snapshot_stale_after_hours}시간을 넘기면 경보`;
}

/* ------------------------------------------------------- engine control */

async function loadTrading() {
  try {
    state.trading = await getJSON("/api/trading/status");
  } catch (err) {
    state.trading = null;
  }
  renderTrading();
}

function renderTrading() {
  const data = state.trading;
  const halted = !!data?.halted;
  const kill = data?.kill_switch || {};

  const button = $("kill-btn");
  if (data === null) {
    button.textContent = "TRADING —";
    button.disabled = true;
    button.className = "bg-surface-container text-on-surface-variant/40 border border-outline-variant/40 px-4 py-1.5 rounded font-label-caps text-label-caps tracking-wide cursor-not-allowed";
  } else {
    button.disabled = false;
    button.textContent = halted ? "RESUME TRADING" : "PAUSE TRADING";
    button.className = halted
      ? "bg-tertiary-container/20 text-tertiary-fixed-dim border border-tertiary-container/40 px-4 py-1.5 rounded font-label-caps text-label-caps tracking-wide hover:bg-tertiary-container/30 transition-colors"
      : "bg-surface-container text-on-surface-variant border border-outline-variant/40 px-4 py-1.5 rounded font-label-caps text-label-caps tracking-wide hover:text-on-surface transition-colors";
  }

  if (!$("ks-state")) return;

  const stateChip = $("engine-state");
  stateChip.textContent = data === null ? "UNKNOWN"
    : halted ? "HALTED" : data.engine_enabled ? "ARMED · PAPER" : "DISABLED";
  stateChip.className = "font-label-caps text-label-caps px-2 py-1 rounded border " + (
    data === null ? "border-outline-variant/40 text-on-surface-variant/50"
      : halted ? "border-tertiary-container/50 text-tertiary-fixed-dim"
      : data.engine_enabled ? "border-secondary-container/50 text-secondary-fixed-dim"
      : "border-outline-variant/40 text-on-surface-variant");

  $("ks-state").textContent = data === null ? "—" : halted ? "발동" : "해제";
  $("ks-state").className = "font-data-mono text-lg font-bold " +
    (halted ? "text-tertiary-fixed-dim" : "text-on-surface");
  $("ks-detail").textContent = halted
    ? [kill.engaged_at ? fmtStamp(kill.engaged_at) : "시각 불명",
       kill.engaged_at_source === "mtime" ? "(파일 수정시각)" : "",
       kill.actor ? `· ${kill.actor}` : "",
       kill.reason ? `· ${kill.reason}` : ""].filter(Boolean).join(" ")
    : kill.path || "—";

  $("engine-enabled").textContent = data === null ? "—" : data.engine_enabled ? "활성" : "비활성";
  $("engine-strategies").textContent = (data?.strategies || []).length
    ? data.strategies.join(", ") : "등록된 전략 없음";
  $("engine-mode").textContent = data === null ? "—" : (data.mode || "paper").toUpperCase();

  const toggle = $("ks-toggle");
  toggle.disabled = data === null;
  toggle.textContent = halted ? "킬 스위치 해제" : "킬 스위치 발동";
  toggle.className = "px-4 py-2 rounded font-label-caps text-label-caps tracking-wide border transition-colors " + (
    halted ? "border-secondary-container/50 text-secondary-fixed-dim hover:bg-secondary-container/10"
           : "border-tertiary-container/50 text-tertiary-fixed-dim hover:bg-tertiary-container/10");
  $("ks-reason").disabled = halted;
}

// Engaging needs no confirmation - stopping is always the safe direction, and
// a stop control that argues with you is a broken stop control. Releasing
// does, because that one starts the engine again.
async function toggleKillSwitch(reason) {
  const halted = !!state.trading?.halted;
  if (halted && !window.confirm("킬 스위치를 해제하고 자동매매 발주를 재개합니다. 계속할까요?")) return;
  const response = await fetch("/api/trading/kill-switch", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ active: !halted, reason: reason || null }),
  });
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  state.trading = await response.json();
  renderTrading();
  const input = $("ks-reason");
  if (input) input.value = "";
  loadOverview().catch(() => {});
  if (state.view === "audit") loadAudit().catch(() => {});
}

/* ------------------------------------------------------------------ init */

function styleRangeButtons() {
  document.querySelectorAll(".range-btn").forEach((button) => {
    const active = button.dataset.range === state.range;
    button.className = "range-btn px-3 py-1 text-[11px] font-data-mono font-bold rounded transition-colors " +
      (active ? "bg-surface border border-outline-variant/50 text-on-surface shadow-sm"
              : "text-on-surface-variant hover:text-on-surface");
  });
}


/* ------------------------------------------------------------- audit log */

const AUDIT_LABELS = {
  baseline: "기준선", universe: "유니버스", strategies: "전략 목록", strategy_params: "전략 파라미터",
  limits: "리스크 한도", veto: "AI 보류", candidate: "AI 제안", kill_switch: "킬 스위치",
};

// How the change time was recovered decides how much it can be trusted, so
// the page says which - a git commit dates the edit exactly and names its
// author, an mtime is the last write to the whole file, and neither is a
// claim about who was sitting at the keyboard.
const AUDIT_METHOD = {
  git: { label: "git 커밋", title: "추적 중인 소스 파일의 마지막 커밋 시각과 작성자입니다. 정확합니다." },
  mtime: { label: "파일 수정시각", title: "파일이 마지막으로 저장된 시각입니다. 이 변경이 아니라 그 파일의 마지막 저장이라, 같은 파일의 다른 부분을 나중에 고쳤다면 그 시각이 찍힙니다." },
  direct: { label: "실행 시점", title: "AI 검토가 방금 만든 항목이라 변경과 감지 사이에 간격이 없습니다." },
};

function fmtStamp(value) {
  return (value || "").replace("T", " ").slice(0, 19);
}

function auditChangeLine(change) {
  const before = change.before === null || change.before === undefined ? "—" : String(change.before);
  const after = change.after === null || change.after === undefined ? "—" : String(change.after);
  const row = document.createElement("div");
  row.className = "flex items-start gap-2 font-data-mono text-xs py-0.5";
  const target = document.createElement("span");
  target.className = "text-on-surface shrink-0";
  target.textContent = change.target;
  const arrow = document.createElement("span");
  arrow.className = "text-on-surface-variant/70 break-all";
  arrow.textContent = `${before} → ${after}`;
  row.append(target, arrow);
  if (change.evidence) {
    const evidence = document.createElement("span");
    evidence.className = "text-on-surface-variant/50 italic break-all";
    evidence.textContent = `(근거: ${change.evidence})`;
    row.appendChild(evidence);
  }
  return row;
}

async function loadAudit() {
  const query = state.auditCategory ? `?category=${state.auditCategory}` : "";
  const data = await getJSON(`/api/audit${query}`);
  const body = $("audit-body");
  body.innerHTML = "";
  const entries = data.entries || [];

  if (!entries.length) {
    body.innerHTML = `<tr><td colspan="5" class="px-4 py-8 text-center text-on-surface-variant">
      기록된 변경이 없습니다. 설정을 바꾼 뒤 <code class="font-data-mono text-primary">python main.py</code>
      또는 <code class="font-data-mono text-primary">python trade.py</code> 를 실행하면 감지됩니다.</td></tr>`;
    return;
  }

  entries.forEach((entry) => {
    const row = document.createElement("tr");
    row.className = "border-b border-outline-variant/20 hover:bg-surface-container-high transition-colors align-top";
    const method = AUDIT_METHOD[entry.changed_by_method];

    // The change time and the basis it was recovered from are one fact in two
    // parts - a bare timestamp invites more trust than an mtime has earned -
    // so they share a cell rather than sitting in columns that can be read apart.
    const whenCell = document.createElement("td");
    whenCell.className = "px-4 py-3 whitespace-nowrap";
    const stamp = document.createElement("div");
    if (entry.changed_at) {
      stamp.className = "font-data-mono text-xs text-on-surface";
      stamp.textContent = fmtStamp(entry.changed_at);
      stamp.title = method ? method.title : "";
    } else {
      // No recoverable change time - say so rather than showing detection
      // time in the slot a reader will read as "when it changed".
      stamp.className = "font-data-mono text-xs text-on-surface-variant/50";
      stamp.textContent = "변경 시각 불명";
    }
    whenCell.appendChild(stamp);
    if (method) {
      const basis = document.createElement("div");
      basis.className = "text-[10px] text-on-surface-variant/50 mt-0.5";
      // The dotted underline is what the header used to say in a sentence:
      // that hovering here explains how exact this timestamp is. A tooltip
      // needing a signpost elsewhere on the page is a tooltip nobody finds -
      // and the affordance sits on the words, not the whole cell, so the
      // help cursor appears where the explanation actually is.
      const label = document.createElement("span");
      label.className =
        "underline decoration-dotted underline-offset-2 decoration-on-surface-variant/40 cursor-help";
      label.textContent = method.label;
      label.title = method.title;
      basis.appendChild(label);
      whenCell.appendChild(basis);
    }

    const categoryCell = document.createElement("td");
    categoryCell.className = "px-4 py-3 whitespace-nowrap";
    const category = document.createElement("span");
    category.className = "text-[10px] px-1.5 py-0.5 rounded border border-outline-variant/50 " +
      "bg-surface-container-highest text-on-surface-variant";
    category.textContent = AUDIT_LABELS[entry.category] || entry.category;
    categoryCell.appendChild(category);

    const whatCell = document.createElement("td");
    whatCell.className = "px-4 py-3";
    const summary = document.createElement("p");
    summary.className = "text-sm text-on-surface";
    summary.textContent = entry.summary || "";
    whatCell.appendChild(summary);
    const changes = entry.changes || [];
    if (changes.length) {
      const detail = document.createElement("div");
      detail.className = "mt-2 border-l-2 border-outline-variant/40 pl-3";
      changes.forEach((change) => detail.appendChild(auditChangeLine(change)));
      whatCell.appendChild(detail);
    }

    const whoCell = document.createElement("td");
    whoCell.className = "px-4 py-3 text-xs text-on-surface-variant/60 whitespace-nowrap";
    const actor = document.createElement("div");
    actor.className = "text-on-surface-variant";
    actor.textContent = entry.actor || "—";
    const source = document.createElement("div");
    source.className = "font-data-mono text-[10px] text-on-surface-variant/50 mt-0.5";
    source.textContent = entry.source || "—";
    whoCell.append(actor, source);

    const detectedCell = document.createElement("td");
    detectedCell.className = "px-4 py-3 text-right font-data-mono text-xs text-on-surface-variant/50 whitespace-nowrap";
    detectedCell.textContent = fmtStamp(entry.detected_at);

    row.append(whenCell, categoryCell, whatCell, whoCell, detectedCell);
    body.appendChild(row);
  });
}

function styleAuditTabs() {
  document.querySelectorAll(".audit-tab").forEach((tab) => {
    const active = (tab.dataset.category || "") === state.auditCategory;
    tab.className = "audit-tab px-3 py-1.5 rounded text-xs font-semibold transition-colors border " +
      (active
        ? "border-primary text-primary bg-surface-container-high"
        : "border-outline-variant/40 text-on-surface-variant hover:text-on-surface");
  });
}

function styleAllocTabs() {
  document.querySelectorAll(".alloc-tab").forEach((tab) => {
    const active = tab.dataset.by === state.allocBy;
    tab.className = "alloc-tab px-4 py-2 font-body-md text-sm transition-colors " +
      (active ? "text-primary border-b-2 border-primary -mb-px" : "text-on-surface-variant hover:text-on-surface");
  });
}

function init() {
  document.querySelectorAll(".nav-item").forEach((item) => {
    item.addEventListener("click", (event) => { event.preventDefault(); setView(item.dataset.view); });
  });
  document.querySelectorAll(".range-btn").forEach((button) => {
    button.addEventListener("click", () => {
      state.range = button.dataset.range;
      styleRangeButtons();
      loadHistory().catch((err) => showError(String(err)));
    });
  });
  document.querySelectorAll(".alloc-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      state.allocBy = tab.dataset.by;
      styleAllocTabs();
      loadAllocation().catch((err) => showError(String(err)));
    });
  });
  $("alert-bell").addEventListener("click", () => setView("overview"));
  const toggleKill = () => {
    const input = $("ks-reason");
    toggleKillSwitch(input && !input.disabled ? input.value.trim() : "")
      .catch((err) => showError(String(err)));
  };
  // The header button is the emergency stop: one click, no dialog, no reason
  // required. Making someone type a justification first is exactly the wrong
  // trade when the reason to stop is that something is going wrong.
  $("kill-btn").addEventListener("click", toggleKill);
  $("ks-toggle").addEventListener("click", toggleKill);
  document.querySelectorAll(".audit-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      state.auditCategory = tab.dataset.category || "";
      styleAuditTabs();
      loadAudit().catch((err) => showError(String(err)));
    });
  });

  const glossary = $("glossary-overlay");
  const closeGlossary = () => { glossary.hidden = true; };
  $("glossary-btn").addEventListener("click", () => { glossary.hidden = false; });
  $("glossary-close").addEventListener("click", closeGlossary);
  glossary.addEventListener("click", (event) => { if (event.target === glossary) closeGlossary(); });
  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !glossary.hidden) closeGlossary();
  });
  // Changing the hash is a same-document navigation, so deep links only work
  // if we listen for it.
  window.addEventListener("hashchange", () => setView(currentHashView()));

  styleRangeButtons();
  styleAllocTabs();
  styleAuditTabs();
  setView(currentHashView());

  const refresh = () => {
    loadOverview().catch((err) => showError(String(err)));
    loadHealth();
    loadTrading();
    if (state.view === "holdings") loadHoldings().catch(() => {});
  };

  refresh();
  loadHistory().catch((err) => showError(String(err)));
  loadAllocation().catch((err) => showError(String(err)));
  loadReports().catch(() => {});

  // The server caches upstream calls, so polling here costs nothing at Toss.
  setInterval(refresh, 15000);
  window.addEventListener("resize", () => { if (state.history) renderChart(state.history); });
}

document.addEventListener("DOMContentLoaded", init);
