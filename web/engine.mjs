/* The production filter engine. This module has no DOM or network dependency. */
export const MAP = { minX: -120, minY: -120, spanX: 14990, spanY: 15100 };
export const BETA_K = 10, MIN_CELL = 5, THIN = 200, MIN_DENSITY = 0.6;
export const ROLLUPS = [128, 64, 32];
const NULL_I16 = -32768;
export const D = { cols: {}, rows: 0, meta: {}, matches: [], players: [], regions: [],
  champions: [], champNames: new Map(), extendedLoaded: false };
export const defaults = () => ({
  layer: 'deaths', subject: { role: [], champ: [], player: [], side: [] },
  opponent: { role: [], champ: [], player: [] }, context: { region: [], cause: [], patch: [] },
  time: null, gold: null, lanegold: null, assists: null,
  grid: 'auto', scale: 'lin', smooth: true,
});
export const S = defaults();
export function resetState() { Object.assign(S, defaults()); }
export const playerIdentity = player => typeof player === 'string' ? player : player?.id || player?.puuid;

export function buildDerived(data = D) {
  const { x, y } = data.cols;
  // Events are binned where they happened. Summoner's Rift is only rotationally
  // similar, not symmetric — the lanes, camps and brush differ side to side —
  // so folding red onto blue would blend cells that are not the same place.
  const mk = size => {
    const cells = new Uint16Array(data.rows);
    for (let i = 0; i < data.rows; i++) {
      const u = (x[i] - MAP.minX) / MAP.spanX, v = (y[i] - MAP.minY) / MAP.spanY;
      const bx = Math.max(0, Math.min(size - 1, Math.floor(u * size)));
      const by = Math.max(0, Math.min(size - 1, Math.floor(v * size)));
      cells[i] = by * size + bx;
    }
    return cells;
  };
  data.cell = {};
  for (const size of ROLLUPS) data.cell[size] = mk(size);
}

export function facetOk(entries, value) {
  let hasIncludes = false, included = false;
  for (const entry of entries) {
    if (entry.neg && entry.v === value) return false;
    if (!entry.neg) { hasIncludes = true; if (entry.v === value) included = true; }
  }
  return !hasIncludes || included;
}

function actorOk(data, filter, i, actor) {
  const c = data.cols;
  for (const key of ['role', 'champ', 'player'])
    if (!facetOk(filter[key], c[`${actor}_${key}`][i])) return false;
  const victimSide = c.flags[i] & 1 ? 200 : 100;
  return !filter.side || facetOk(filter.side, actor === 'victim' ? victimSide : 300 - victimSide);
}

export function scan(size, data = D, state = S) {
  if (state.lanegold && !data.extendedLoaded) throw new Error('Lane gold data is still loading.');
  const c = data.cols, deaths = new Uint32Array(size * size), kills = new Uint32Array(size * size);
  const cells = data.cell[size];
  const zones = new Map(), matchesD = new Set(), matchesK = new Set();
  let nD = 0, nK = 0;
  const within = (value, range) => !range || (value >= range[0] && value <= range[1]);
  const laneOk = (i, actor) => !state.lanegold ||
    (c[`${actor}_gold_diff_lane`][i] !== NULL_I16 && within(c[`${actor}_gold_diff_lane`][i], state.lanegold));
  for (let i = 0; i < data.rows; i++) {
    if (!within(c.second[i], state.time) || !within(c.team_gold_diff[i], state.gold)
      || !within(c.assists[i], state.assists)) continue;
    if (!facetOk(state.context.region, c.region_sk[i]) || !facetOk(state.context.cause, c.cause[i])
      || !facetOk(state.context.patch, c.patch_sk[i])) continue;
    const asVictim = actorOk(data, state.subject, i, 'victim')
      && actorOk(data, state.opponent, i, 'killer') && laneOk(i, 'victim');
    const asKiller = c.cause[i] === 0 && actorOk(data, state.subject, i, 'killer')
      && actorOk(data, state.opponent, i, 'victim') && laneOk(i, 'killer');
    if (!asVictim && !asKiller) continue;
    const region = c.region_sk[i];
    if (!zones.has(region)) zones.set(region, { region, deaths: 0, kills: 0 });
    const zone = zones.get(region);
    if (asVictim) { deaths[cells[i]]++; nD++; zone.deaths++; matchesD.add(c.match_sk[i]); }
    if (asKiller) { kills[cells[i]]++; nK++; zone.kills++; matchesK.add(c.match_sk[i]); }
  }
  const nMatch = state.layer === 'deaths' ? matchesD.size : state.layer === 'kills' ? matchesK.size
    : new Set([...matchesD, ...matchesK]).size;
  return { deaths, kills, nD, nK, nMatch, size, zones: [...zones.values()] };
}

export function pct(arr, p) {
  const values = Array.from(arr).filter(x => x > 0).sort((a, b) => a - b);
  return values.length ? values[Math.min(values.length - 1, Math.floor(values.length * p))] : 0;
}

export function layerField(result, state = S) {
  const { deaths, kills, size } = result, out = new Float32Array(size * size);
  if (state.layer === 'deaths' || state.layer === 'kills') {
    const src = state.layer === 'deaths' ? deaths : kills;
    const total = state.layer === 'deaths' ? result.nD : result.nK;
    if (total) for (let i = 0; i < out.length; i++) out[i] = src[i] / total;
    return { out, kind: 'share', lo: 0, hi: pct(out, 0.99) };
  }
  // `evidence` converts smoothed local means back to the event counts the
  // prior and the MIN_CELL floor are defined against; it is 1 for raw counts.
  const w = result.evidence || 1;
  for (let i = 0; i < out.length; i++) {
    const d = deaths[i] * w, k = kills[i] * w;
    const danger = (d + BETA_K / 2) / (d + k + BETA_K);
    out[i] = d + k < MIN_CELL ? NaN : state.layer === 'danger' ? danger : 1 - danger;
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
          const id = entry.id || playerIdentity(data.players[entry.v]);
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
