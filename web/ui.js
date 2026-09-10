import { D, S, apply, loadBundle, loadExtended, loadMapImage, loadChampionNames,
  readURL, resetState, schedule } from './app.js';
import { playerLabel, playerName, playerTag } from './data.mjs';
import { CollectionController, ACTIVE, runDescription } from './collection.mjs';

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const OPTIONS = {
  role: () => (D.meta.roles || []).map((name, i) => ({ v: i, label: name })),
  champ: () => D.champions.map(id => ({ v: id, label: D.champNames.get(id) || `Champion ${id}` })),
  // label is the in-game name; sub is the tagline, shown dimmed and searchable
  // so the ~1% of ladder names that collide stay distinguishable.
  player: () => D.players.map((player, i) =>
    ({ v: i, label: playerName(player), sub: playerTag(player), full: playerLabel(player) })),
  side: () => [{ v: 100, label: 'Blue side' }, { v: 200, label: 'Red side' }],
  region: () => D.regions.map((region, i) => ({ v: i, label: region.name })),
  cause: () => (D.meta.causes || []).map((name, i) => ({ v: i, label: name })),
  patch: () => (D.meta.patches || []).map((name, i) => ({ v: i, label: name })),
};
const LABELS = { role: 'Role', champ: 'Champion', player: 'Player', side: 'Side',
  region: 'Zone', cause: 'Death cause', patch: 'Patch' };
const ALIAS = {
  "kai'sa": 'kaisa', 'kaisa': "kai'sa", 'nunu & willump': 'nunu',
  'j4': 'jarvan iv', 'asol': 'aurelion sol', 'mundo': 'dr. mundo',
  'kha': "kha'zix", 'vel': "vel'koz", 'cho': "cho'gath", 'rek': "rek'sai",
  'mf': 'miss fortune', 'tf': 'twisted fate', 'ww': 'warwick', 'yi': 'master yi',
  'lb': 'leblanc', 'gp': 'gangplank', 'ez': 'ezreal', 'sett': 'sett',
};
function fuzzy(q, items) {
  q = q.trim().toLowerCase();
  if (!q) return items.slice(0, 60);
  const alias = ALIAS[q];
  const norm = s => s.toLowerCase().replace(/[^a-z0-9#]/g, '');
  const nq = norm(q);
  const score = it => {
    const full = optionFull(it).toLowerCase();
    const l = it.label.toLowerCase(), n = norm(l), nf = norm(full);
    if (l === q || full === q || (alias && l === alias)) return 0;
    if (n.startsWith(nq)) return 1;
    if (n.includes(nq) || nf.startsWith(nq)) return 2;
    let i = 0;                                   // subsequence match
    for (const ch of nf) if (ch === nq[i]) i++;
    return i === nq.length ? 3 : 99;
  };
  return items.map(it => [score(it), it]).filter(([s]) => s < 99)
    .sort((a, b) => a[0] - b[0] || a[1].label.length - b[1].label.length)
    .slice(0, 60).map(([, it]) => it);
}

const optionFull = option => option.full || option.label + (option.sub || '');
/* The tagline is real information, not decoration: it is dimmed rather than
 * dropped so two players called "Tree" never collapse into one filter pill. */
function writeOption(el, option, prefix = '') {
  el.replaceChildren(prefix + option.label);
  if (!option.sub) return;
  const sub = document.createElement('span');
  sub.className = 'tagline'; sub.textContent = option.sub;
  el.append(sub);
}

function renderFacet(el) {
  const group = el.closest('[data-group]').dataset.group, facet = el.dataset.facet;
  const entries = S[group][facet], options = OPTIONS[facet]();
  el.replaceChildren();
  const head = document.createElement('div'), label = document.createElement('span');
  head.className = 'fhead'; label.className = 'fname'; label.textContent = LABELS[facet];
  const add = document.createElement('button'); add.className = 'addbtn'; add.textContent = '+ add';
  add.setAttribute('aria-label', `Add ${group} ${LABELS[facet].toLowerCase()} filter`);
  head.append(label, add); el.append(head);
  const pills = document.createElement('div'); pills.className = 'pills';
  for (const [i, entry] of entries.entries()) {
    const option = options.find(option => option.v === entry.v) || { label: String(entry.v) };
    const name = optionFull(option);
    const pill = document.createElement('span'); pill.className = `pill${entry.neg ? ' neg' : ''}`;
    const toggle = document.createElement('button'); toggle.className = 'lbl';
    writeOption(toggle, option, entry.neg ? '¬ ' : '');
    toggle.title = `${entry.neg ? 'Include' : 'Exclude'} ${name}`;
    toggle.setAttribute('aria-label', toggle.title);
    toggle.onclick = () => { entry.neg = !entry.neg; renderFacet(el); schedule(); };
    const remove = document.createElement('button'); remove.className = 'x'; remove.textContent = '×';
    remove.setAttribute('aria-label', `Remove ${name} filter`);
    remove.onclick = () => { entries.splice(i, 1); renderFacet(el); schedule(); };
    pill.append(toggle, remove); pills.append(pill);
  }
  el.append(pills); add.onclick = () => openTypeahead(el, group, facet, options);
}

let closeTypeahead = () => {};
function openTypeahead(el, group, facet, options) {
  closeTypeahead();
  const wrap = document.createElement('div'); wrap.className = 'typeahead';
  const input = document.createElement('input'), menu = document.createElement('div');
  input.placeholder = `Search ${LABELS[facet].toLowerCase()}…`;
  input.setAttribute('aria-label', `Search ${group} ${LABELS[facet].toLowerCase()}`);
  menu.className = 'menu'; wrap.append(input, menu); el.append(wrap); input.focus();
  let selected = 0, shown = [];
  const close = () => { wrap.remove(); document.removeEventListener('pointerdown', outside); };
  const outside = event => { if (!wrap.contains(event.target)) close(); };
  closeTypeahead = close;
  const pick = option => {
    const entries = S[group][facet];
    if (!entries.some(entry => entry.v === option.v)) entries.push({ v: option.v, neg: false });
    close(); renderFacet(el); $('.addbtn', el).focus(); schedule();
  };
  const draw = () => {
    shown = fuzzy(input.value, options); selected = Math.max(0, Math.min(selected, shown.length - 1));
    menu.replaceChildren();
    shown.forEach((option, i) => {
      const button = document.createElement('button'); writeOption(button, option);
      button.title = optionFull(option);
      button.className = i === selected ? 'sel' : '';
      button.onclick = () => pick(option); menu.append(button);
    });
    if (!shown.length) { const empty = document.createElement('p'); empty.textContent = 'No matches.'; menu.append(empty); }
    $('.sel', menu)?.scrollIntoView({ block: 'nearest' });
  };
  input.oninput = () => { selected = 0; draw(); };
  input.onkeydown = event => {
    if (event.key === 'Escape') { close(); $('.addbtn', el).focus(); }
    else if (event.key === 'ArrowDown') { selected++; draw(); event.preventDefault(); }
    else if (event.key === 'ArrowUp') { selected--; draw(); event.preventDefault(); }
    else if (event.key === 'Enter' && shown[selected]) { pick(shown[selected]); event.preventDefault(); }
  };
  document.addEventListener('pointerdown', outside); draw();
}

let filterLoading = null;
function ensureExtended() {
  if (D.extendedLoaded) return Promise.resolve();
  if (filterLoading) return filterLoading;
  const message = $('#filterMessage'); message.hidden = false;
  $('span', message).textContent = 'Loading lane gold data. The map will update when it is ready.';
  $('#filterRetry').hidden = true;
  filterLoading = loadExtended().then(() => {
    message.hidden = true; schedule();
  }).catch(() => {
    $('span', message).textContent = 'Lane gold data could not be loaded. The previous map is still shown.';
    $('#filterRetry').hidden = false;
  }).finally(() => { filterLoading = null; });
  return filterLoading;
}

function makeSlider(id, key, min, max, format, step = 1, onUse = null) {
  const root = $(id), track = $('.track', root), fill = $('.fill', root);
  const lo = $('.hd.lo', root), hi = $('.hd.hi', root), value = $('.val', root);
  let a = Math.min(max, Math.max(min, S[key]?.[0] ?? min));
  let b = Math.min(max, Math.max(a, S[key]?.[1] ?? max));
  const position = v => (v - min) / (max - min);
  const draw = () => {
    lo.style.left = `${position(a) * 100}%`; hi.style.left = `${position(b) * 100}%`;
    fill.style.left = `${position(a) * 100}%`; fill.style.width = `${(position(b) - position(a)) * 100}%`;
    value.textContent = a === min && b === max ? 'all' : `${format(a)} – ${format(b)}`;
    S[key] = a === min && b === max ? null : [a, b];
    for (const [handle, current, lower, upper] of [[lo, a, min, b], [hi, b, a, max]]) {
      handle.setAttribute('role', 'slider'); handle.setAttribute('aria-valuemin', lower);
      handle.setAttribute('aria-valuemax', upper); handle.setAttribute('aria-valuenow', current);
      handle.setAttribute('aria-valuetext', format(current));
      handle.setAttribute('aria-label', `${key === 'lanegold' ? 'Subject lane gold difference' : key === 'gold' ? 'Team gold difference' : key === 'time' ? 'Game time' : 'Fight assists'} ${handle === lo ? 'minimum' : 'maximum'}`);
    }
  };
  const changed = () => {
    draw();
    if (S[key] && onUse) void onUse();
    if (!S.lanegold) $('#filterMessage').hidden = true;
    schedule();
  };
  const fromX = x => {
    const bounds = track.getBoundingClientRect();
    return Math.min(max, Math.max(min, Math.round((min + Math.max(0, Math.min(1,
      (x - bounds.left) / bounds.width)) * (max - min)) / step) * step));
  };
  const setHandle = (which, next) => { if (which === 'lo') a = Math.min(next, b); else b = Math.max(next, a); changed(); };
  const down = (event, which) => {
    event.preventDefault(); (which === 'lo' ? lo : hi).focus();
    setHandle(which, fromX(event.clientX));
    const move = next => setHandle(which, fromX(next.clientX));
    const up = () => {
      window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', up);
      window.removeEventListener('pointercancel', up);
    };
    window.addEventListener('pointermove', move); window.addEventListener('pointerup', up);
    window.addEventListener('pointercancel', up);
  };
  lo.onpointerdown = event => { event.stopPropagation(); down(event, 'lo'); };
  hi.onpointerdown = event => { event.stopPropagation(); down(event, 'hi'); };
  track.onpointerdown = event => down(event, Math.abs(fromX(event.clientX) - a) < Math.abs(fromX(event.clientX) - b) ? 'lo' : 'hi');
  for (const [handle, which] of [[lo, 'lo'], [hi, 'hi']]) handle.onkeydown = event => {
    const keys = { ArrowLeft: -step, ArrowDown: -step, ArrowRight: step, ArrowUp: step, PageDown: -step * 10, PageUp: step * 10 };
    let next = which === 'lo' ? a : b;
    if (event.key === 'Home') next = min;
    else if (event.key === 'End') next = max;
    else if (event.key in keys) next += keys[event.key];
    else return;
    event.preventDefault(); setHandle(which, Math.min(max, Math.max(min, next)));
  };
  draw();
  return { reset: () => { a = min; b = max; draw(); },
    setWindow: (x, y) => { a = Math.min(max, Math.max(min, x)); b = Math.min(max, Math.max(a, y)); changed(); } };
}
const mmss = seconds => `${Math.floor(seconds / 60)}:${String(Math.round(seconds % 60)).padStart(2, '0')}`;
const kgold = gold => `${gold > 0 ? '+' : ''}${(gold / 1000).toFixed(1)}k`;

function wireTooltip() {
  const canvas = $('#map'), tip = $('#tip');
  canvas.onmousemove = event => {
    const bounds = canvas.getBoundingClientRect(), size = D.lastSize, result = D.lastResult;
    if (!result) return;
    const bx = Math.max(0, Math.min(size - 1, Math.floor((event.clientX - bounds.left) / bounds.width * size)));
    const by = Math.max(0, Math.min(size - 1, Math.floor((1 - (event.clientY - bounds.top) / bounds.height) * size)));
    const i = by * size + bx, d = result.deaths[i], k = result.kills[i];
    if (!d && !k) { tip.hidden = true; return; }
    // Counts are always this cell's exact events. The ratio follows whatever
    // the map drew, which is the smoothed neighbourhood when Smooth is on, so
    // the suppression notice agrees with what is or is not painted.
    const shown = D.lastShown || result;
    const sd = shown.deaths[i], sk = shown.kills[i];
    const danger = (sd + 5) / (sd + sk + 10);
    tip.textContent = `Map cell · ${d} deaths · ${k} kills\n`
      + `Danger ${danger.toFixed(2)} · Opportunity ${(1 - danger).toFixed(2)}`
      + (S.smooth ? ' (smoothed)' : '')
      + (sd + sk < 5 ? '\nRatio suppressed: fewer than 5 events' : '');
    tip.hidden = false;
    tip.style.left = `${Math.max(0, Math.min(event.clientX - bounds.left + 12, bounds.width - tip.offsetWidth))}px`;
    tip.style.top = `${Math.max(0, Math.min(event.clientY - bounds.top + 12, bounds.height - tip.offsetHeight))}px`;
  };
  canvas.onmouseleave = () => { tip.hidden = true; };
}

let timer = null, sliders = {}, maxSeconds = 600;
function stopPlayback() {
  clearInterval(timer); timer = null;
  $('#sl-time .play').classList.remove('on'); $('#sl-time .play').setAttribute('aria-label', 'Play game-time window');
}
function syncControls() {
  $$('#layers input').forEach(input => { input.checked = input.value === S.layer; });
  $$('#gridSeg button').forEach(button => { button.classList.toggle('on', button.dataset.g === S.grid); button.setAttribute('aria-pressed', button.dataset.g === S.grid); });
  $$('#scaleSeg button').forEach(button => { button.classList.toggle('on', button.dataset.s === S.scale); button.setAttribute('aria-pressed', button.dataset.s === S.scale); });
  $('#smoothChk').checked = S.smooth;
}
function setupDatasetControls() {
  stopPlayback(); closeTypeahead();
  $$('.facet').forEach(renderFacet);
  maxSeconds = D.matches.reduce((max, match) => Math.max(max, match.duration || 0), 600);
  sliders = {
    time: makeSlider('#sl-time', 'time', 0, maxSeconds, mmss, 15),
    gold: makeSlider('#sl-gold', 'gold', -25000, 25000, kgold, 250),
    lanegold: makeSlider('#sl-lanegold', 'lanegold', -10000, 10000, kgold, 100, ensureExtended),
    assists: makeSlider('#sl-assists', 'assists', 0, 4, v => v === 0 ? 'solo' : `${v}`, 1),
  };
  syncControls(); $$('[data-controls]').forEach(el => { el.inert = false; });
}
function wireControls() {
  $$('#layers input').forEach(input => { input.onchange = () => { S.layer = input.value; schedule(); }; });
  $$('#gridSeg button').forEach(button => { button.onclick = () => { S.grid = button.dataset.g; syncControls(); schedule(); }; });
  $$('#scaleSeg button').forEach(button => { button.onclick = () => { S.scale = button.dataset.s; syncControls(); schedule(); }; });
  $('#smoothChk').onchange = event => { S.smooth = event.target.checked; schedule(); };
  $('#themeBtn').onclick = () => { document.documentElement.dataset.theme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'; };
  $('#resetBtn').onclick = () => {
    stopPlayback(); resetState(); Object.values(sliders).forEach(slider => slider.reset());
    $$('.facet').forEach(renderFacet); syncControls(); $('#filterMessage').hidden = true; schedule();
  };
  $('#filterRetry').onclick = () => void ensureExtended();
  const play = $('#sl-time .play'); play.setAttribute('aria-label', 'Play game-time window');
  play.onclick = () => {
    if (timer) { stopPlayback(); return; }
    let start = S.time?.[0] || 0; const width = 240;
    play.classList.add('on'); play.setAttribute('aria-label', 'Pause game-time window');
    timer = setInterval(() => { start += 20; if (start + width > maxSeconds) start = 0; sliders.time.setWindow(start, start + width); }, 120);
  };
  wireTooltip();
}

let refreshPromise = null;
async function refreshDataset() {
  if (refreshPromise) return refreshPromise;
  refreshPromise = (async () => {
    if (!D.dataset_id) { $('#loading .spin').hidden = false; $('#loadingText').textContent = 'Loading real dataset…'; }
    try {
      const changed = await loadBundle();
      if (changed) setupDatasetControls();
      $('#loading').classList.add('done'); $('#filterMessage').hidden = true;
      const date = value => new Date(Number(value) * 1000).toLocaleDateString();
      const window = D.meta.collected_from && D.meta.collected_to
        ? ` · ${date(D.meta.collected_from)}–${date(D.meta.collected_to)}` : '';
      $('#datasetInfo').textContent = `Real Riot data · ${D.matches.length.toLocaleString()} games${window} · release ${D.dataset_id}`;
      schedule();
    } catch (error) {
      if (D.dataset_id) {
        $('#datasetInfo').textContent = `Showing ${D.matches.length.toLocaleString()} real games from release ${D.dataset_id}. Updated data is temporarily unavailable.`;
      } else {
        $('#loading .spin').hidden = true;
        $('#loadingText').textContent = error.code === 'empty'
          ? 'No real dataset collected yet. Use Update game data to collect games.'
          : error.message || 'Real data could not be loaded. Please retry.';
        $('#datasetRetry').hidden = false;
        $('#nStat').textContent = 'No real dataset available';
      }
    }
  })().finally(() => { refreshPromise = null; });
  return refreshPromise;
}

function showCollection(controller) {
  const { run, dataset } = controller.value;
  $$('.pocButtons button').forEach(button => {
    button.disabled = controller.busy || !!(controller.pending && controller.pending.mode !== button.dataset.mode);
  });
  $('#runStatus').textContent = runDescription(run);
  const progress = $('#runProgress'); progress.hidden = !run || !ACTIVE.has(run.status);
  if (run && run.target != null) { progress.max = Math.max(1, Number(run.target)); progress.value = Math.max(0, Number(run.new_games) || 0); }
  else progress.removeAttribute('value');
  $('#runTime').textContent = run?.updated_at ? `Updated ${new Date(run.updated_at * 1000).toLocaleTimeString()}` : '';
  $('#runError').textContent = controller.message || (typeof run?.error === 'string' ? run.error : '');
  const version = dataset?.dataset_id || run?.published_version;
  if (version && version !== D.dataset_id) void refreshDataset();
}

async function boot() {
  readURL(); wireControls(); $$('[data-controls]').forEach(el => { el.inert = true; });
  const controller = new CollectionController({ changed: showCollection });
  $$('.pocButtons button').forEach(button => { button.onclick = () => void controller.start(button.dataset.mode); });
  $('#datasetRetry').onclick = () => void refreshDataset();
  const poll = async () => { await controller.poll(); setTimeout(poll, controller.busy ? 5000 : 15000); };
  void poll();
  await Promise.all([loadChampionNames(), loadMapImage()]);
  await refreshDataset();
  window.addEventListener('popstate', async () => {
    readURL();
    if (D.dataset_id) { setupDatasetControls(); if (S.lanegold) await ensureExtended(); schedule(); }
  });
}
void boot();
