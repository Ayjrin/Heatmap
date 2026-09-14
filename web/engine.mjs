/* The production filter engine. This module has no DOM or network dependency. */
export const MAP = { minX: -120, minY: -120, spanX: 14990, spanY: 15100 };
export const BETA_K = 10, MIN_CELL = 5, THIN = 200, MIN_DENSITY = 0.6;
export const ROLLUPS = [128, 64, 32];
export const CELL_GRID = 128;
export const UNKNOWN_TIER = 255;
const NULL_I16 = -32768;
/* A release is a set of partitions, one per (cohort tier, patch). Each part
 * carries its own rows once loaded; `cells` holds the prerendered default
 * view per tier so the map paints before any part arrives. */
export const D = { manifest: null, base: '', meta: {}, regions: [], champions: [], champNames: new Map(),
  tiers: [], tierNames: [], cells: new Map(), parts: new Map(), players: null,
  matchCount: 0, maxDuration: 0, loading: null, lastResult: null, lastShown: null };
export const emptyDataset = () => ({ manifest: null, base: '', meta: {}, regions: [], champions: [],
  champNames: new Map(), tiers: [], tierNames: [], cells: new Map(), parts: new Map(), players: null,
  matchCount: 0, maxDuration: 0, loading: null, lastResult: null, lastShown: null });
export const defaults = () => ({
  layer: 'deaths', subject: { role: [], champ: [], player: [], side: [], tier: [] },
  opponent: { role: [], champ: [], player: [], tier: [] }, context: { region: [], cause: [], patch: [], tier: [] },
  time: null, gold: null, lanegold: null, assists: null,
  grid: 'auto', scale: 'lin', smooth: true,
});
export const S = defaults();
export function resetState() { Object.assign(S, defaults()); }
export const playerIdentity = player => typeof player === 'string' ? player : player?.id || player?.puuid;

export function binIndex(x, y, size) {
  const u = (x - MAP.minX) / MAP.spanX, v = (y - MAP.minY) / MAP.spanY;
  const bx = Math.max(0, Math.min(size - 1, Math.floor(u * size)));
  const by = Math.max(0, Math.min(size - 1, Math.floor(v * size)));
  return by * size + bx;
}

/* Bin one partition's rows at every rollup. Events are binned where they
 * happened. Summoner's Rift is only rotationally similar, not symmetric — the
 * lanes, camps and brush differ side to side — so folding red onto blue would
 * blend cells that are not the same place. */
export function buildDerived(part) {
  const { x, y } = part.cols;
  part.cell = {};
  for (const size of ROLLUPS) {
    const cells = new Uint16Array(part.rows);
    for (let i = 0; i < part.rows; i++) cells[i] = binIndex(x[i], y[i], size);
    part.cell[size] = cells;
  }
}

export function facetOk(entries, value) {
  let hasIncludes = false, included = false;
  for (const entry of entries) {
    if (entry.neg && entry.v === value) return false;
    if (!entry.neg) { hasIncludes = true; if (entry.v === value) included = true; }
  }
  return !hasIncludes || included;
}

export function partSelected(part, state = S) {
  return facetOk(state.context.tier, part.tier) && facetOk(state.context.patch, part.patch);
}

function actorOk(c, filter, i, actor) {
  for (const key of ['role', 'champ', 'player', 'tier'])
    if (!facetOk(filter[key], c[`${actor}_${key}`][i])) return false;
  const victimSide = c.flags[i] & 1 ? 200 : 100;
  return !filter.side || facetOk(filter.side, actor === 'victim' ? victimSide : 300 - victimSide);
}

const within = (value, range) => !range || (value >= range[0] && value <= range[1]);

export function scan(size, data = D, state = S) {
  const deaths = new Uint32Array(size * size), kills = new Uint32Array(size * size);
  // Distinct games per cell and per zone. A part stores its rows grouped by
  // match (dataset.py orders every wire column by match_sk, and data.mjs
  // checks that on load), so remembering the last game seen counts each one
  // once without a Set per cell — 16,384 Sets per scan is not affordable, and
  // a count of events alone cannot tell one game's massacre from fifty games
  // trading once each. Parts are visited one after another and each part's
  // games are offset by its `matchBase`, so identities never collide.
  const games = new Uint32Array(size * size), lastGame = new Int32Array(size * size).fill(-1);
  const zones = new Map(), matchesD = new Set(), matchesK = new Set();
  let nD = 0, nK = 0;
  for (const part of data.parts.values()) {
    if (!part.loaded || !partSelected(part, state)) continue;
    if (state.lanegold && !part.extendedLoaded) throw new Error('Lane gold data is still loading.');
    const c = part.cols, cells = part.cell[size], base = part.matchBase;
    const laneOk = (i, actor) => !state.lanegold ||
      (c[`${actor}_gold_diff_lane`][i] !== NULL_I16 && within(c[`${actor}_gold_diff_lane`][i], state.lanegold));
    for (let i = 0; i < part.rows; i++) {
      if (!within(c.second[i], state.time) || !within(c.team_gold_diff[i], state.gold)
        || !within(c.assists[i], state.assists)) continue;
      if (!facetOk(state.context.region, c.region_sk[i]) || !facetOk(state.context.cause, c.cause[i])) continue;
      const asVictim = actorOk(c, state.subject, i, 'victim')
        && actorOk(c, state.opponent, i, 'killer') && laneOk(i, 'victim');
      const asKiller = c.cause[i] === 0 && actorOk(c, state.subject, i, 'killer')
        && actorOk(c, state.opponent, i, 'victim') && laneOk(i, 'killer');
      if (!asVictim && !asKiller) continue;
      const region = c.region_sk[i], match = base + c.match_sk[i];
      if (!zones.has(region)) zones.set(region, { region, deaths: 0, kills: 0, games: 0, lastGame: -1 });
      const zone = zones.get(region);
      if (asVictim) { deaths[cells[i]]++; nD++; zone.deaths++; matchesD.add(match); }
      if (asKiller) { kills[cells[i]]++; nK++; zone.kills++; matchesK.add(match); }
      if (lastGame[cells[i]] !== match) { lastGame[cells[i]] = match; games[cells[i]]++; }
      if (zone.lastGame !== match) { zone.lastGame = match; zone.games++; }
    }
  }
  const nMatch = state.layer === 'deaths' ? matchesD.size : state.layer === 'kills' ? matchesK.size
    : new Set([...matchesD, ...matchesK]).size;
  // `lastGame` is scratch for the distinct-game count; it is not part of a zone.
  return { deaths, kills, games, nD, nK, nMatch, size,
    zones: [...zones.values()].map(({ lastGame: _, ...zone }) => zone) };
}

/* The prerendered cells answer exactly the default view: every filter empty
 * except, optionally, the context rank. Anything else needs the rows. */
export function cellsAnswer(state = S) {
  const facets = [...Object.values(state.subject), ...Object.values(state.opponent),
    state.context.region, state.context.cause, state.context.patch];
  return facets.every(entries => entries.length === 0)
    && !state.time && !state.gold && !state.lanegold && !state.assists;
}

/* Sum the selected tiers' sections. Tiers are disjoint sets of games, so
 * deaths, kills, distinct games and match totals all add. Deaths and kills
 * roll up by 4:1 summation because the 128 grid nests exactly inside 64 and
 * 32; distinct games do not nest, so each rollup reads its own stored count.
 * Must return exactly what scan() returns for the same state. */
export function scanCells(size, data = D, state = S) {
  const deaths = new Uint32Array(size * size), kills = new Uint32Array(size * size);
  const games = new Uint32Array(size * size), zones = new Map();
  const factor = CELL_GRID / size;
  let nD = 0, nK = 0, matchesD = 0, matchesK = 0;
  for (const [tier, section] of data.cells) {
    if (!facetOk(state.context.tier, tier)) continue;
    const fine = section.cols[`deaths_${CELL_GRID}`], fineKills = section.cols[`kills_${CELL_GRID}`];
    for (let i = 0; i < CELL_GRID * CELL_GRID; i++) {
      if (!fine[i] && !fineKills[i]) continue;
      const bx = i % CELL_GRID, by = (i / CELL_GRID) | 0;
      const cell = ((by / factor) | 0) * size + ((bx / factor) | 0);
      deaths[cell] += fine[i]; kills[cell] += fineKills[i];
    }
    const stored = section.cols[`games_${size}`];
    for (let i = 0; i < games.length; i++) games[i] += stored[i];
    const zd = section.cols.zone_deaths, zk = section.cols.zone_kills, zg = section.cols.zone_games;
    for (let region = 0; region < zd.length; region++) {
      if (!zd[region] && !zk[region]) continue;
      if (!zones.has(region)) zones.set(region, { region, deaths: 0, kills: 0, games: 0 });
      const zone = zones.get(region);
      zone.deaths += zd[region]; zone.kills += zk[region]; zone.games += zg[region];
    }
    nD += section.rows; nK += section.kills; matchesD += section.death_matches; matchesK += section.kill_matches;
  }
  // Every champion kill is also a death, so the union behind the ratio layers
  // is the death set.
  const nMatch = state.layer === 'kills' ? matchesK : matchesD;
  return { deaths, kills, games, nD, nK, nMatch, size, zones: [...zones.values()], prerendered: true };
}

export function pct(arr, p) {
  const values = Array.from(arr).filter(x => x > 0).sort((a, b) => a - b);
  return values.length ? values[Math.min(values.length - 1, Math.floor(values.length * p))] : 0;
}

/* One cell of an already-drawn grid. `evidence` converts a smoothed local mean
 * back to the event count the prior and the MIN_CELL floor are defined against;
 * it is 1 for raw counts. Both the renderer and the tooltip go through these,
 * so the number quoted for a cell is always the number that was painted. */
export const cellEvents = (result, i) => (result.deaths[i] + result.kills[i]) * (result.evidence || 1);
export function cellDanger(result, i) {
  const w = result.evidence || 1, d = result.deaths[i] * w, k = result.kills[i] * w;
  return (d + BETA_K / 2) / (d + k + BETA_K);
}

export function layerField(result, state = S) {
  const { deaths, kills, size } = result, out = new Float32Array(size * size);
  if (state.layer === 'deaths' || state.layer === 'kills') {
    const src = state.layer === 'deaths' ? deaths : kills;
    const total = state.layer === 'deaths' ? result.nD : result.nK;
    if (total) for (let i = 0; i < out.length; i++) out[i] = src[i] / total;
    return { out, kind: 'share', lo: 0, hi: pct(out, 0.99) };
  }
  for (let i = 0; i < out.length; i++) {
    const danger = cellDanger(result, i);
    out[i] = cellEvents(result, i) < MIN_CELL ? NaN : state.layer === 'danger' ? danger : 1 - danger;
  }
  return { out, kind: 'ratio', lo: 0, hi: 1 };
}

export function zoneSummary(result, layer = S.layer) {
  const total = layer === 'kills' ? result.nK : result.nD;
  return result.zones.map(zone => {
    const n = zone.deaths + zone.kills;
    const danger = (zone.deaths + BETA_K / 2) / (n + BETA_K);
    const value = layer === 'kills' ? zone.kills / (total || 1)
      : layer === 'deaths' ? zone.deaths / (total || 1)
      : n < MIN_CELL ? NaN : layer === 'danger' ? danger : 1 - danger;
    return { ...zone, n, value };
  }).filter(zone => Number.isFinite(zone.value) && zone.value > 0)
    .sort((a, b) => b.value - a.value || b.n - a.n);
}

const ranges = { time: [0, 86400], gold: [-25000, 25000], lanegold: [-10000, 10000], assists: [0, 9] };
export function parseState(search) {
  const state = defaults(), params = new URLSearchParams(search);
  for (const [key, values] of Object.entries({ layer: ['deaths', 'kills', 'danger', 'opportunity'],
    grid: ['auto', '128', '64', '32'], scale: ['lin', 'sqrt', 'log'] })) {
    if (values.includes(params.get(key))) state[key] = params.get(key);
  }
  for (const [group, short] of [['subject', 's'], ['opponent', 'o'], ['context', 'c']]) {
    for (const facet of Object.keys(state[group])) {
      // Dictionary indices change between releases. Player links carry PUUIDs.
      // Rank ordinals are stable across releases (TIER_NAMES is append-only).
      if (facet === 'player') {
        state[group].player = [false, true].flatMap(neg =>
          params.getAll(`${short}.${neg ? 'exclude_player_id' : 'player_id'}`)
            .filter(id => /^[A-Za-z0-9_-]{1,256}$/.test(id))
            .map(id => ({ v: -1, id, neg })));
        continue;
      }
      const raw = params.get(`${short}.${facet}`);
      if (!raw) continue;
      state[group][facet] = raw.split(',').filter(token => /^!?\d+$/.test(token)).map(token =>
        ({ v: Number(token.replace('!', '')), neg: token.startsWith('!') }))
        .filter(entry => Number.isSafeInteger(entry.v));
    }
  }
  for (const [key, [min, max]] of Object.entries(ranges)) {
    const raw = params.get(key), pair = raw?.split(',').map(Number);
    if (pair?.length === 2 && pair.every(Number.isFinite) && pair[0] <= pair[1])
      state[key] = pair.map(value => Math.min(max, Math.max(min, value)));
  }
  state.smooth = params.get('smooth') !== '0';
  return state;
}

export function stateQuery(state = S, data = D) {
  const params = new URLSearchParams({ layer: state.layer });
  for (const [group, short] of [['subject', 's'], ['opponent', 'o'], ['context', 'c']])
    for (const [facet, entries] of Object.entries(state[group])) {
      if (facet === 'player') {
        for (const entry of entries) {
          const id = entry.id || data.players?.idOf?.(entry.v);
          if (id) params.append(`${short}.${entry.neg ? 'exclude_player_id' : 'player_id'}`, id);
        }
      } else if (entries.length) {
        params.set(`${short}.${facet}`, entries.map(e => `${e.neg ? '!' : ''}${e.v}`).join(','));
      }
    }
  for (const key of Object.keys(ranges)) if (state[key]) params.set(key, state[key].join(','));
  if (state.grid !== 'auto') params.set('grid', state.grid);
  if (state.scale !== 'lin') params.set('scale', state.scale);
  if (!state.smooth) params.set('smooth', '0');
  return params.toString();
}
