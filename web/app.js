/* Browser rendering for the shared, tested aggregation engine. */
import { D, S, MAP, BETA_K, MIN_CELL, THIN, MIN_DENSITY, ROLLUPS,
  scan, layerField, pct, zoneSummary, parseState, stateQuery, resetState } from './engine.mjs';
import { loadBundle, loadExtended, loadChampionNames, reconcileFilters } from './data.mjs';
const $ = (selector, root = document) => root.querySelector(selector);

/* Separable Gaussian, 9-tap. Always applied to raw counts, never to a value
 * derived from them: Danger blurs its deaths and kills grids separately and
 * only then forms the ratio. Blurring the ratio itself would weight a cell of
 * one event the same as a cell of fifty, which is wrong wherever density
 * varies sharply — and near the map edges it always does. */
const KERNEL = [0.0276, 0.0663, 0.1238, 0.1802, 0.2042, 0.1802, 0.1238, 0.0663, 0.0276];
/* The kernel sums to 1, so a blurred cell holds a local MEAN, not a total.
 * 1/Σw² is that mean's effective sample size — about 46 cells here — and it is
 * the factor that turns the mean back into "events this cell summarizes".
 * Both the Beta prior and the MIN_CELL floor are calibrated against event
 * counts, so both must see that number rather than the mean; otherwise ten
 * imaginary trades outweigh a whole neighbourhood and every smoothed ratio
 * cell collapses to 0.5. */
const NEIGHBOURHOOD = 1 / KERNEL.reduce((sum, w) => sum + w * w, 0) ** 2;

function blur(src, size) {
  const k = KERNEL;
  const tmp = new Float32Array(src.length), out = new Float32Array(src.length);
  for (let y = 0; y < size; y++) for (let x = 0; x < size; x++) {
    let s = 0;
    for (let j = -4; j <= 4; j++) {
      const xx = x + j; if (xx < 0 || xx >= size) continue;
      s += src[y * size + xx] * k[j + 4];
    }
    tmp[y * size + x] = s;
  }
  for (let y = 0; y < size; y++) for (let x = 0; x < size; x++) {
    let s = 0;
    for (let j = -4; j <= 4; j++) {
      const yy = y + j; if (yy < 0 || yy >= size) continue;
      s += tmp[yy * size + x] * k[j + 4];
    }
    out[y * size + x] = s;
  }
  return out;
}

/* --------------------------------------------------------------- palettes */
const INFERNO = [[0,0,4],[22,11,57],[66,10,104],[106,23,110],[147,38,103],
  [188,55,84],[221,81,58],[243,120,25],[252,165,10],[246,215,70],[252,255,164]];
const DIVERGE = [[46,120,220],[104,160,232],[168,196,238],[214,222,232],[224,224,224],
  [238,214,206],[240,170,150],[232,110,96],[210,50,60]];

function lut(stops, n = 256) {
  const out = new Uint8ClampedArray(n * 3);
  for (let i = 0; i < n; i++) {
    const t = i / (n - 1) * (stops.length - 1), a = Math.floor(t), b = Math.min(a + 1, stops.length - 1);
    const f = t - a;
    for (let c = 0; c < 3; c++) out[i * 3 + c] = stops[a][c] + (stops[b][c] - stops[a][c]) * f;
  }
  return out;
}
const LUT_SEQ = lut(INFERNO), LUT_DIV = lut(DIVERGE);

/* --------------------------------------------------------------- renderer */
const mapCanvas = $('#map'), mctx = mapCanvas.getContext('2d');
const off = document.createElement('canvas'), octx = off.getContext('2d');
let mapImage = null;

async function loadMapImage() {
  const img = new Image();
  try {
    await new Promise((resolve, reject) => {
      img.onload = resolve; img.onerror = reject; img.src = 'assets/map11.png';
    });
    mapImage = img;
  } catch { /* Pinned art is optional; gameplay data never has an invented fallback. */ }
}

function render(result) {
  const size = result.size;
  // Smoothing happens on the counts, so every layer — including the Danger
  // and Opportunity ratios — gets it. Sharing one blurred source also keeps
  // the density mask and the drawn value derived from the same numbers.
  const shown = S.smooth
    ? { ...result, deaths: blur(result.deaths, size), kills: blur(result.kills, size),
        evidence: NEIGHBOURHOOD }
    : result;
  D.lastShown = shown;                   // the tooltip reports what was drawn
  const { out: field, kind, hi } = layerField(shown);
  const ratio = kind === 'ratio';
  const hiV = ratio ? 1 : hi;
  // A ratio layer carries its confidence in alpha rather than in colour.
  // Unsmoothed, the cell's own event count drives that directly. Smoothed,
  // density is a continuous field covering the whole map, so it is ramped
  // against its own p99 the way the count layers are — a flat floor would
  // paint every cell alike and bury both the map art and the signal.
  const density = ratio ? new Float32Array(size * size) : null;
  if (density) for (let i = 0; i < density.length; i++) density[i] = shown.deaths[i] + shown.kills[i];
  const denseHi = density && S.smooth ? pct(density, 0.99) || 1 : 1;
  const drawn = i => density[i] * (shown.evidence || 1) >= MIN_CELL;
  // A danger ratio almost never leaves 0.4-0.6 once the noise is averaged out,
  // so painting it across the full 0-1 ramp renders every cell the same
  // near-neutral grey. Keep 0.5 pinned to the ramp's midpoint, because on a
  // diverging scale the midpoint has to stay "even", and stretch the span to
  // the drawn data. The legend prints the resulting endpoints, so the stretch
  // is stated rather than silently implied.
  let spread = 0.5;
  if (ratio) {
    const devs = [];
    for (let i = 0; i < field.length; i++)
      if (!Number.isNaN(field[i]) && drawn(i)) devs.push(Math.abs(field[i] - 0.5));
    devs.sort((a, b) => a - b);
    if (devs.length) spread = Math.max(0.02, devs[Math.floor((devs.length - 1) * 0.98)]);
  }

  off.width = off.height = size;
  const img = octx.createImageData(size, size);
  const LUTv = ratio ? LUT_DIV : LUT_SEQ;

  for (let i = 0; i < size * size; i++) {
    const v = field[i];
    let a = 0, idx = 0;
    if (Number.isNaN(v)) { a = 0; }
    else if (ratio) {
      if (!drawn(i)) a = 0;
      else {
        const t = 0.5 + (v - 0.5) / (2 * spread);
        idx = Math.max(0, Math.min(255, Math.round(t * 255)));
        a = S.smooth ? Math.round(Math.pow(Math.min(1, density[i] / denseHi), 0.65) * 255)
          : Math.min(255, 90 + density[i] * 12);
      }
    } else if (v > 0 && hiV > 0) {
      let t = v / hiV;
      if (S.scale === 'sqrt') t = Math.sqrt(t);
      else if (S.scale === 'log') t = Math.log1p(t * 9) / Math.log(10);
      t = Math.max(0, Math.min(1, t));
      idx = Math.round(t * 255);
      a = Math.round(Math.pow(t, 0.65) * 255);   // ramp alpha so sparse fades
    }
    // Y flips exactly once, here: game origin is bottom-left, canvas top-left.
    const gx = i % size, gy = (size - 1 - ((i / size) | 0));
    const o = (gy * size + gx) * 4;
    img.data[o] = LUTv[idx * 3]; img.data[o + 1] = LUTv[idx * 3 + 1];
    img.data[o + 2] = LUTv[idx * 3 + 2]; img.data[o + 3] = a;
  }
  octx.putImageData(img, 0, 0);

  const W = mapCanvas.width, H = mapCanvas.height;
  mctx.clearRect(0, 0, W, H);
  mctx.fillStyle = '#0a0d12'; mctx.fillRect(0, 0, W, H);
  if (mapImage) { mctx.globalAlpha = 0.55; mctx.drawImage(mapImage, 0, 0, W, H); mctx.globalAlpha = 1; }
  mctx.imageSmoothingEnabled = true;
  mctx.drawImage(off, 0, 0, W, H);
  drawLegend(ratio, ratio ? spread : hiV, result);
}

/* `scale` is the p99 share for a count layer and the half-span around 0.5 for
 * a ratio layer; both are whatever the renderer just clipped the ramp to. */
function drawLegend(ratio, scale, r) {
  const c = $('#ramp'), x = c.getContext('2d');
  const img = x.createImageData(c.width, 1);
  const L = ratio ? LUT_DIV : LUT_SEQ;
  for (let i = 0; i < c.width; i++) {
    const idx = Math.round(i / (c.width - 1) * 255);
    img.data[i * 4] = L[idx * 3]; img.data[i * 4 + 1] = L[idx * 3 + 1];
    img.data[i * 4 + 2] = L[idx * 3 + 2]; img.data[i * 4 + 3] = 255;
  }
  x.putImageData(img, 0, 0);
  x.drawImage(c, 0, 0, c.width, 1, 0, 0, c.width, c.height);
  const names = { deaths: 'Deaths', kills: 'Kills', danger: 'Danger', opportunity: 'Opportunity' };
  $('#lgTitle').textContent = names[S.layer];
  if (ratio) {
    $('#lgLo').textContent = `${(0.5 - scale).toFixed(2)} · ${S.layer === 'danger' ? 'wins fights' : 'loses fights'}`;
    $('#lgHi').textContent = `${S.layer === 'danger' ? 'loses fights' : 'wins fights'} · ${(0.5 + scale).toFixed(2)}`;
    $('#lgNote').textContent =
      `${S.layer === 'danger' ? '(D+5)/(D+K+10)' : '(K+5)/(D+K+10)'}, with a Beta(5,5) prior. 0.5 = even, `
      + `and the ramp is stretched to the range actually present. `
      + `Cells with fewer than ${MIN_CELL} ${S.smooth ? 'smoothed ' : ''}events are not drawn.`;
  } else {
    $('#lgLo').textContent = '0';
    $('#lgHi').textContent = `${(scale * 100).toFixed(2)}% of ${S.layer}`;
    $('#lgNote').textContent =
      'Share of the filtered total, clipped at p99 so one objective cell does '
      + 'not flatten the map.';
  }
}

/* ------------------------------------------------------------------ zones */
function renderZones(result) {
  const list = $('#zoneList'); list.replaceChildren();
  const summary = zoneSummary(result), ratio = ['danger', 'opportunity'].includes(S.layer);
  $('#zoneNote').textContent = ratio
    ? 'Zone ratios use exact event totals; at least 5 events per zone.'
    : 'Share of events in each map zone. Zone totals are always exact, never smoothed.';
  for (const zone of summary.slice(0, 8)) {
    const name = D.regions[zone.region]?.name || 'Unlabelled';
    const item = document.createElement('li'), label = document.createElement('span');
    label.className = 'nm'; label.textContent = name; label.title = name;
    const bar = document.createElement('span'), fill = document.createElement('i');
    bar.className = 'bar'; fill.style.width = `${zone.value / (summary[0].value || 1) * 100}%`;
    bar.append(fill);
    const value = document.createElement('span'); value.className = 'num';
    value.textContent = ratio ? zone.value.toFixed(2) : `${(zone.value * 100).toFixed(1)}%`;
    item.append(label, bar, value); list.append(item);
  }
  if (!summary.length) {
    const item = document.createElement('li'); item.textContent = 'No matching events.'; list.append(item);
  }
}

/* ------------------------------------------------------------------ apply */
let pending = null;
function schedule() { if (!pending) pending = requestAnimationFrame(apply); }

function apply() {
  pending = null;
  if (!D.dataset_id || (S.lanegold && !D.extendedLoaded)) return;
  let size = S.grid === 'auto' ? 128 : +S.grid;
  let r = scan(size);
  const n = S.layer === 'kills' ? r.nK : S.layer === 'deaths' ? r.nD : r.nD + r.nK;
  if (S.grid === 'auto') {
    // Count layers are share-normalized and blurred, so they read fine when
    // sparse. Ratio layers SUPPRESS every cell under MIN_CELL events, so they
    // need DENSITY, not just a large n — 1,278 events spread over 16,384 cells
    // leaves almost every cell blank and the map reads as empty even though
    // the slice is perfectly healthy. Pick the finest grid that still puts
    // enough events per cell to clear the floor.
    const ratio = S.layer === 'danger' || S.layer === 'opportunity';
    const target = ratio
      ? (ROLLUPS.find(sz => n / (sz * sz) >= MIN_DENSITY) || 32)
      : (n < THIN / 4 ? 32 : n < THIN ? 64 : 128);
    if (target !== size) { size = target; r = scan(size); }
  }
  D.lastResult = r; D.lastSize = size;   // the tooltip reads these
  render(r);
  renderZones(r);

  const nn = S.layer === 'kills' ? r.nK : S.layer === 'deaths' ? r.nD : r.nD + r.nK;
  const auto = S.grid === 'auto' && size !== 128;
  $('#gridStat').textContent = `${size}² · ${(MAP.spanX / size) | 0} units/cell`
    + (auto ? ' · auto-coarsened for density' : '');
  const thin = $('#thinWarn');
  thin.hidden = nn >= THIN;
  if (!thin.hidden) thin.textContent =
    nn === 0 ? 'No events match these filters.' : `Small sample${auto ? ` · ${size}² grid` : ''}`;

  const ratio = S.layer === 'danger' || S.layer === 'opportunity';
  const anySubject = Object.values(S.subject).some(a => a.length);
  $('#layerHint').textContent = (ratio && !anySubject)
    ? 'Choose a role, champion or player to compare subject kills and deaths. All-player ratios are largely balanced; executions can shift them.'
    : '';
  syncURL();
}

function syncURL() { history.replaceState(null, '', '?' + stateQuery()); }
function readURL() {
  Object.assign(S, parseState(location.search));
  if (D.dataset_id) reconcileFilters({}, D);
}

export { D, S, scan, apply, loadBundle, loadExtended, loadMapImage, loadChampionNames,
  readURL, layerField, resetState, schedule };
