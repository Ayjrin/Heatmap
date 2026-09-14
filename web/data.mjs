import { D, S, CELL_GRID, ROLLUPS, UNKNOWN_TIER, buildDerived, partSelected } from './engine.mjs';

export class DatasetError extends Error {
  constructor(message, code = 'invalid') { super(message); this.code = code; }
}

/* Parts up to this many bytes beyond the selected ones load on their own;
 * anything larger waits until a filter actually needs it. */
export const AUTO_LOAD_BYTES = 64 * 1024 * 1024;
export const PART_CONCURRENCY = 3;
const CHECK_BATCH = 25000;
// Release files live at most two directories deep (parts/<key>/core.bin) and
// never step outside the release.
const FILE = /^[\w.-]+(\/[\w.-]+){0,2}$/;
const safeFile = name => typeof name === 'string' && FILE.test(name) && !name.split('/').includes('..');

async function response(url, fetcher = fetch) {
  const result = await fetcher(url, { cache: 'no-cache' });
  if (!result.ok) throw new DatasetError(result.status === 404
    ? 'No dataset has been published yet.' : 'The dataset could not be loaded. Please retry.',
  result.status === 404 ? 'empty' : 'network');
  return result;
}

async function json(url, fetcher) { return (await response(url, fetcher)).json(); }

// Manifest sizes describe decoded bytes, so progress stays accurate even when
// CloudFront compresses the response or omits Content-Length.
async function binary(url, part, fetcher, progress = () => {}) {
  if (!Number.isSafeInteger(part.bytes) || part.bytes < 0)
    throw new DatasetError('The dataset size is invalid.');
  if (part.bytes === 0) { progress(0, 0); return new ArrayBuffer(0); }
  const result = await response(url, fetcher);
  if (!result.body) throw new DatasetError('The dataset download is incomplete. Please retry.');
  const reader = result.body.getReader(), bytes = new Uint8Array(part.bytes);
  let loaded = 0;
  try {
    progress(0, part.bytes);
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      if (loaded + value.byteLength > bytes.length)
        throw new DatasetError('The dataset download size is invalid.');
      bytes.set(value, loaded); loaded += value.byteLength;
      progress(loaded, part.bytes);
    }
    if (loaded !== part.bytes) throw new DatasetError('The dataset download is incomplete. Please retry.');
    return bytes.buffer;
  } catch (error) {
    await reader.cancel().catch(() => {});
    throw error;
  } finally { reader.releaseLock(); }
}

const TYPES = { u8: Uint8Array, i8: Int8Array, u16: Uint16Array, i16: Int16Array, u32: Uint32Array, i32: Int32Array };

/* Typed-array views onto one downloaded buffer. `rows` pins every column to
 * one length; pass null when columns legitimately differ (the cells file). */
function views(buffer, part, rows = null) {
  if (buffer.byteLength !== part.bytes) throw new DatasetError('The dataset download is incomplete. Please retry.');
  const cols = {};
  if (!Array.isArray(part.columns)) throw new DatasetError('The dataset schema is invalid.');
  for (const column of part.columns) {
    const type = TYPES[column.type];
    if (!type || typeof column.name !== 'string' || Object.hasOwn(cols, column.name)
      || !Number.isSafeInteger(column.offset) || column.offset < 0
      || !Number.isSafeInteger(column.length) || column.length < 0
      || (rows !== null && column.length !== rows)
      || column.offset % type.BYTES_PER_ELEMENT !== 0
      || column.offset + column.length * type.BYTES_PER_ELEMENT > buffer.byteLength)
      throw new DatasetError('The dataset schema is invalid.');
    cols[column.name] = new type(buffer, column.offset, column.length);
  }
  return cols;
}

export function validatePointer(pointer) {
  if (pointer?.source_kind !== 'riot' || typeof pointer.dataset_id !== 'string'
    || !/^[A-Za-z0-9_-]+$/.test(pointer.dataset_id)
    || pointer.base !== `data/releases/${pointer.dataset_id}`)
    throw new DatasetError('This dataset does not have verified Riot provenance.');
  return pointer;
}

const requiredCore = ['match_sk', 'x', 'y', 'second', 'region_sk', 'patch_sk', 'cause', 'assists',
  'flags', 'team_gold_diff', 'victim_champ', 'victim_role', 'victim_player', 'victim_tier',
  'killer_champ', 'killer_role', 'killer_player', 'killer_tier'];
const requiredCells = ['deaths_128', 'kills_128', ...ROLLUPS.map(size => `games_${size}`),
  'zone_deaths', 'zone_kills', 'zone_games'];

/* Names and PUUIDs are sharded by dictionary index; `player_map` is ordered
 * by PUUID, so a shard is a contiguous sorted range and a PUUID is found by
 * binary search over the first id of every shard, then inside that shard.
 * Nothing is fetched until a label or a link asks for it. */
export class PlayerIndex {
  constructor(base, index, tiers, fetcher) {
    if (!Number.isSafeInteger(index?.count) || index.count < 0 || !Number.isSafeInteger(index.shard_size)
      || index.shard_size <= 0 || !Array.isArray(index.first_id)
      || index.first_id.length !== Math.ceil(index.count / index.shard_size)
      || index.first_id.some((id, i) => typeof id !== 'string' || (i > 0 && !(index.first_id[i - 1] < id))))
      throw new DatasetError('The player index is invalid.');
    if (tiers.length !== index.count) throw new DatasetError('The player rank table does not match the index.');
    this.base = base; this.count = index.count; this.shardSize = index.shard_size;
    this.firstIds = index.first_id; this.shards = index.first_id.length; this.tiers = tiers;
    this.fetcher = fetcher; this.names = new Map(); this.ids = new Map(); this.byId = new Map();
    this.pending = new Map();
  }
  shardOf(v) { return Math.floor(v / this.shardSize); }
  valid(v) { return Number.isSafeInteger(v) && v >= 0 && v < this.count; }
  shard(kind, k) {
    const store = kind === 'names' ? this.names : this.ids;
    if (store.has(k)) return Promise.resolve(store.get(k));
    const key = `${kind}-${k}`;
    if (!this.pending.has(key)) {
      const expected = Math.min(this.shardSize, this.count - k * this.shardSize);
      this.pending.set(key, json(`${this.base}/players/${key}.json`, this.fetcher).then(values => {
        if (!Array.isArray(values) || values.length !== expected || values.some(value => typeof value !== 'string'))
          throw new DatasetError('A player shard is invalid.');
        store.set(k, values);
        if (kind === 'ids') values.forEach((id, i) => this.byId.set(id, k * this.shardSize + i));
        return values;
      }).finally(() => this.pending.delete(key)));
    }
    return this.pending.get(key);
  }
  ensureNames(k) { return this.shard('names', k); }
  ensureIds(k) { return this.shard('ids', k); }
  /* Dictionary index of a PUUID, or -1. Costs at most one shard fetch. */
  async resolve(id) {
    if (this.byId.has(id)) return this.byId.get(id);
    if (!this.count || id < this.firstIds[0]) return -1;
    let lo = 0, hi = this.shards - 1;
    while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (this.firstIds[mid] <= id) lo = mid; else hi = mid - 1; }
    const ids = await this.ensureIds(lo);
    let a = 0, b = ids.length - 1;
    while (a <= b) {
      const mid = (a + b) >> 1;
      if (ids[mid] === id) return lo * this.shardSize + mid;
      if (ids[mid] < id) a = mid + 1; else b = mid - 1;
    }
    return -1;
  }
  nameOf(v) { return this.valid(v) ? this.names.get(this.shardOf(v))?.[v % this.shardSize] ?? null : null; }
  idOf(v) { return this.valid(v) ? this.ids.get(this.shardOf(v))?.[v % this.shardSize] ?? null : null; }
  tierOf(v) { return this.valid(v) ? this.tiers[v] : UNKNOWN_TIER; }
  get namesLoaded() { return this.names.size; }
  async loadAllNames(onShard = () => {}) {
    for (let k = 0; k < this.shards; k++) { await this.ensureNames(k); onShard(k + 1, this.shards); }
  }
}

/* Riot IDs are stored whole ("Sneaky#NA1") because only the pair is unique.
 * The game name alone is what a player is called in game and what anyone
 * actually types, so it leads every label and the tagline trails it, kept
 * because ~1% of ladder names collide and the tag is the only tiebreak. */
export const playerLabel = player => typeof player === 'string' ? player
  : player?.name || player?.label || player?.riot_id || player?.id || 'Unknown player';
const splitRiotId = player => {
  const full = playerLabel(player), hash = full.lastIndexOf('#');
  return hash > 0 ? [full.slice(0, hash), full.slice(hash)] : [full, ''];
};
export const playerName = player => splitRiotId(player)[0];
export const playerTag = player => splitRiotId(player)[1];

/* Surrogate indices may change between releases; retain selections by identity.
 * Player entries must already carry their PUUID resolved against `next`. */
export function reconcileFilters(previous, next, state = S) {
  const remap = (entries, oldValues, newValues, identity = value => value) => {
    const indices = new Map(newValues.map((value, i) => [identity(value), i]));
    return entries.flatMap(entry => {
      const value = oldValues[entry.v];
      if (value === undefined) return [];
      const index = indices.get(identity(value));
      return index === undefined ? [] : [{ ...entry, v: index }];
    });
  };
  for (const group of ['subject', 'opponent']) {
    state[group].player = state[group].player.flatMap(entry => {
      const id = entry.id || previous.players?.idOf?.(entry.v);
      const index = id ? next.players.byId.get(id) : undefined;
      return index === undefined ? [] : [{ ...entry, id, v: index }];
    });
  }
  if (previous.dataset_id) {
    state.context.patch = remap(state.context.patch, previous.meta.patches || [], next.meta.patches || []);
    state.context.region = remap(state.context.region, previous.regions, next.regions, value => value.name);
  }
  const actorTiers = new Set([...(next.tierNames || []).map((_, i) => i), UNKNOWN_TIER]);
  const allowed = {
    role: new Set((next.meta.roles || []).map((_, i) => i)), champ: new Set(next.champions),
    player: new Set(), side: new Set([100, 200]),
    region: new Set(next.regions.map((_, i) => i)),
    cause: new Set((next.meta.causes || []).map((_, i) => i)),
    patch: new Set((next.meta.patches || []).map((_, i) => i)),
  };
  for (const group of ['subject', 'opponent', 'context'])
    for (const facet of Object.keys(state[group])) {
      const ok = facet === 'player' ? entry => next.players.valid(entry.v)
        : facet === 'tier' ? entry => (group === 'context' ? next.tiers.includes(entry.v) : actorTiers.has(entry.v))
        : entry => allowed[facet].has(entry.v);
      state[group][facet] = state[group][facet].filter(ok);
    }
}

function checkManifest(manifest, pointer) {
  if (manifest?.version !== 2) throw new DatasetError('This release format is not supported by this page. Reload to pick up the current version.', 'unsupported');
  if (manifest.meta?.source_kind !== 'riot') throw new DatasetError('This dataset does not have verified Riot provenance.');
  if (manifest.meta.dataset_id && manifest.meta.dataset_id !== pointer.dataset_id)
    throw new DatasetError('The dataset version does not match its release.');
  const meta = manifest.meta;
  if (!Number.isSafeInteger(manifest.rows) || manifest.rows < 0 || !Array.isArray(manifest.parts)
    || !manifest.cells || !safeFile(manifest.cells.file) || !manifest.players || !safeFile(manifest.players.index)
    || !safeFile(manifest.players.tiers?.file) || !Array.isArray(meta.tier_names) || !Array.isArray(meta.tiers)
    || !Array.isArray(meta.patches) || !Number.isSafeInteger(meta.match_count) || meta.match_count < 0)
    throw new DatasetError('The dataset is empty or invalid.');
  if (!meta.tier_names.every(name => typeof name === 'string') || meta.tier_names.length >= UNKNOWN_TIER
    || !meta.tiers.every(tier => Number.isSafeInteger(tier) && (tier === UNKNOWN_TIER || tier < meta.tier_names.length)))
    throw new DatasetError('The dataset rank table is invalid.');
  let matchBase = 0, rows = 0;
  const keys = new Set();
  for (const part of manifest.parts) {
    if (typeof part.key !== 'string' || keys.has(part.key) || !Number.isSafeInteger(part.rows) || part.rows < 0
      || !Number.isSafeInteger(part.matches) || part.matches < 1 || !meta.tiers.includes(part.tier)
      || !Number.isSafeInteger(part.patch) || part.patch < 0 || part.patch >= meta.patches.length
      || !safeFile(part.core?.file) || !safeFile(part.extended?.file)
      || !Number.isSafeInteger(part.core.bytes) || !Number.isSafeInteger(part.extended.bytes))
      throw new DatasetError('The dataset partitions are invalid.');
    keys.add(part.key); part.matchBase = matchBase; matchBase += part.matches; rows += part.rows;
  }
  if (rows !== manifest.rows || matchBase !== meta.match_count) throw new DatasetError('The dataset partitions do not add up.');
  return manifest;
}

function cellSections(buffer, manifest, regions) {
  const cells = new Map();
  const grid = CELL_GRID * CELL_GRID;
  if (manifest.cells.grid !== CELL_GRID || !Array.isArray(manifest.cells.tiers)
    || manifest.cells.regions !== regions) throw new DatasetError('The prerendered cells are invalid.');
  for (const section of manifest.cells.tiers) {
    if (!manifest.meta.tiers.includes(section.tier) || cells.has(section.tier)
      || ['rows', 'kills', 'death_matches', 'kill_matches', 'matches'].some(k => !Number.isSafeInteger(section[k]) || section[k] < 0))
      throw new DatasetError('The prerendered cells are invalid.');
    const cols = views(buffer, { bytes: buffer.byteLength, columns: section.columns });
    if (requiredCells.some(name => !cols[name]) || cols.deaths_128.length !== grid || cols.kills_128.length !== grid
      || ROLLUPS.some(size => cols[`games_${size}`].length !== size * size)
      || ['zone_deaths', 'zone_kills', 'zone_games'].some(name => cols[name].length !== regions))
      throw new DatasetError('The prerendered cells are invalid.');
    let deaths = 0, kills = 0;
    for (let i = 0; i < grid; i++) { deaths += cols.deaths_128[i]; kills += cols.kills_128[i]; }
    if (deaths !== section.rows || kills !== section.kills || section.kills > section.rows
      || section.kill_matches > section.death_matches || section.death_matches > section.matches)
      throw new DatasetError('The prerendered cells do not reconcile.');
    cells.set(section.tier, { ...section, cols });
  }
  return cells;
}

/* Every per-row invariant the engine relies on. Yields between batches so
 * progress can paint; runs per part as it lands. */
async function checkRows(cols, part, data, paint) {
  const players = data.players.count, regions = data.regions.length, tiers = data.tierNames.length;
  let previous = -1;
  for (let i = 0; i < part.rows; i++) {
    if (cols.match_sk[i] >= part.matches || cols.region_sk[i] >= regions || cols.patch_sk[i] !== part.patch
      || cols.victim_player[i] >= players
      || (cols.cause[i] === 0 && cols.killer_player[i] >= players)
      || (cols.victim_tier[i] >= tiers && cols.victim_tier[i] !== UNKNOWN_TIER)
      || (cols.killer_tier[i] >= tiers && cols.killer_tier[i] !== UNKNOWN_TIER))
      throw new DatasetError('The dataset references invalid metadata.');
    // Rows arrive ordered by match, which is what lets a scan count the
    // distinct games behind a cell without a Set per cell. Check the property
    // rather than assume it: a release that ever stopped grouping its rows
    // would silently inflate every per-cell and per-zone game count.
    if (cols.match_sk[i] < previous) throw new DatasetError('The dataset rows are not grouped by game.');
    previous = cols.match_sk[i];
    if (i > 0 && i % CHECK_BATCH === 0) await paint();
  }
}

let loading = null;   // { dataset, fetcher, report, onMerge, queue, active, failed, promise, resolve }

function selectionOrder(parts, state) {
  const bytes = part => part.core.bytes;
  const selected = parts.filter(part => partSelected(part, state)).sort((a, b) => bytes(a) - bytes(b));
  const rest = parts.filter(part => !partSelected(part, state)).sort((a, b) => bytes(a) - bytes(b));
  let budget = AUTO_LOAD_BYTES;
  const automatic = [];
  for (const part of rest) { if (bytes(part) > budget) break; budget -= bytes(part); automatic.push(part); }
  return [...selected, ...automatic];
}

async function loadPart(part, job) {
  const data = D;
  const paint = () => job.report ? new Promise(resolve => setTimeout(resolve, 0)) : Promise.resolve();
  const buffer = await binary(`${data.base}/${part.core.file}`, part.core, job.fetcher,
    (loaded, total) => job.report('partition', loaded, total, part));
  const cols = views(buffer, part.core, part.rows);
  if (requiredCore.some(name => !cols[name])) throw new DatasetError('The dataset columns are incomplete.');
  await checkRows(cols, part, data, paint);
  const staged = { ...part, cols, extendedLoaded: false };
  buildDerived(staged);
  if (S.lanegold) { Object.assign(staged.cols, await extendedFor(staged, job.fetcher)); staged.extendedLoaded = true; }
  if (D.dataset_id !== job.dataset) throw new DatasetError('The dataset changed while loading.', 'stale');
  Object.assign(part, staged, { loaded: true });
  return part;
}

function pump(job) {
  while (job.active < PART_CONCURRENCY && job.queue.length) {
    const part = job.queue.shift();
    if (part.loaded || part.inflight) continue;
    part.inflight = true; job.active++;
    loadPart(part, job).then(() => {
      job.failed.delete(part.key);
      job.report('partition', 0, 0, part, true);
      job.onMerge?.(part);
    }).catch(error => {
      if (error?.code === 'stale' || D.dataset_id !== job.dataset) return;
      job.failed.set(part.key, error);
      job.report('partition-failed', 0, 0, part);
    }).finally(() => {
      part.inflight = false; job.active--;
      if (D.dataset_id === job.dataset) pump(job);
      if (!job.active && !job.queue.length) job.settle();
    });
  }
  if (!job.active && !job.queue.length) job.settle();
}

function startPartitions(fetcher, report, onMerge) {
  const job = { dataset: D.dataset_id, fetcher, report, onMerge, queue: [], active: 0, failed: new Map() };
  job.promise = new Promise(resolve => { job.settle = () => { if (!job.active && !job.queue.length) resolve(); }; });
  job.queue = selectionOrder([...D.parts.values()], S);
  loading = job;
  pump(job);
  return job;
}

/* Resolves once every queued partition has either loaded or failed. */
export function partitionsSettled() { return loading?.promise ?? Promise.resolve(); }
export function partitionFailures() { return loading ? [...loading.failed.keys()] : []; }
export function partsLoaded(state = S) {
  const selected = [...D.parts.values()].filter(part => partSelected(part, state));
  return { loaded: selected.filter(part => part.loaded).length, total: selected.length,
    rows: selected.reduce((sum, part) => sum + (part.loaded ? part.rows : 0), 0) };
}

/* Queue every selected partition that is not loaded, including ones beyond
 * the automatic size policy and ones that failed earlier. */
export function ensurePartitions(state = S) {
  if (!loading || loading.dataset !== D.dataset_id) return Promise.resolve();
  const wanted = [...D.parts.values()].filter(part => partSelected(part, state) && !part.loaded && !part.inflight
    && !loading.queue.includes(part));
  if (!wanted.length) return loading.promise;
  if (!loading.queue.length && !loading.active) {
    // The previous batch settled; reopen the job so callers can await this one.
    loading.promise = new Promise(resolve => { loading.settle = () => { if (!loading.active && !loading.queue.length) resolve(); }; });
  }
  loading.queue.unshift(...wanted);
  for (const part of wanted) loading.failed.delete(part.key);
  pump(loading);
  return loading.promise;
}

async function extendedFor(part, fetcher, progress = () => {}) {
  const descriptor = part.extended;
  if (!descriptor || !safeFile(descriptor.file)) throw new DatasetError('The advanced-filter dataset is missing.');
  const buffer = await binary(`${D.base}/${descriptor.file}`, descriptor, fetcher, progress);
  const cols = views(buffer, descriptor, part.rows);
  if (!cols.victim_gold_diff_lane || !cols.killer_gold_diff_lane)
    throw new DatasetError('The lane gold columns are missing.');
  return cols;
}

/* Lane gold columns for every loaded part that lacks them. One request per
 * part, deduplicated while in flight; parts loaded later fetch theirs eagerly
 * while the lane gold filter is set. */
export function loadExtended(fetcher = fetch, progress = () => {}) {
  const dataset = D.dataset_id;
  const pending = [...D.parts.values()].filter(part => part.loaded && !part.extendedLoaded).map(part => {
    if (part.extendedPromise) return part.extendedPromise;
    part.extendedPromise = extendedFor(part, fetcher, progress).then(cols => {
      if (D.dataset_id !== dataset) throw new DatasetError('The dataset changed. Please retry the filter.');
      Object.assign(part.cols, cols); part.extendedLoaded = true;
    }).finally(() => { part.extendedPromise = null; });
    return part.extendedPromise;
  });
  return Promise.all(pending).then(() => undefined);
}
export const extendedReady = (state = S) => [...D.parts.values()].every(part => !part.loaded || !partSelected(part, state) || part.extendedLoaded);

/* Commit only a complete validated release; failures leave the previous map usable.
 * Returns once the map can paint from the prerendered cells; partitions keep
 * streaming in the background and `onMerge` fires as each one lands. */
export async function loadBundle(fetcher = fetch, onProgress = null, onMerge = null) {
  const report = (stage, loaded = 0, total = null, part = null, merged = false) =>
    onProgress?.({ stage, loaded, total, part: part?.key ?? null, merged, parts: partsLoaded() });
  report('metadata');
  const pointer = validatePointer(await json('data/current.json', fetcher));
  if (pointer.dataset_id === D.dataset_id) return false;
  const base = pointer.base;
  const manifest = checkManifest(await json(`${base}/manifest.json`, fetcher), pointer);
  const [regions, champions, index, tierBytes] = await Promise.all([
    json(`${base}/regions.json`, fetcher), json(`${base}/champions.json`, fetcher),
    json(`${base}/${manifest.players.index}`, fetcher),
    binary(`${base}/${manifest.players.tiers.file}`, manifest.players.tiers, fetcher),
  ]);
  if (!Array.isArray(regions) || !Array.isArray(champions) || !regions.length
    || regions.some(region => typeof region?.name !== 'string')
    || new Set(regions.map(region => region.name)).size !== regions.length)
    throw new DatasetError('The dataset metadata is incomplete.');
  const players = new PlayerIndex(base, index, new Uint8Array(tierBytes), fetcher);
  // Player links carry PUUIDs; each costs at most one ids shard.
  await Promise.all(['subject', 'opponent'].flatMap(group => S[group].player.filter(entry => entry.id)
    .map(entry => players.resolve(entry.id))));
  report('cells');
  const cellBuffer = await binary(`${base}/${manifest.cells.file}`, manifest.cells, fetcher,
    (loaded, total) => report('cells', loaded, total));
  const cells = cellSections(cellBuffer, manifest, regions.length);
  const parts = new Map(manifest.parts.map(part => [part.key, { ...part, loaded: false, inflight: false,
    cols: null, cell: null, extendedLoaded: false, extendedPromise: null }]));
  const next = { manifest, base, dataset_id: pointer.dataset_id, meta: manifest.meta, regions, champions,
    tiers: manifest.meta.tiers, tierNames: manifest.meta.tier_names, cells, parts, players,
    matchCount: manifest.meta.match_count, maxDuration: Number(manifest.meta.max_duration) || 0,
    lastResult: null, lastShown: null };
  reconcileFilters(D, next);
  Object.assign(D, next);
  report('ready');
  D.loading = startPartitions(fetcher, report, onMerge);
  return true;
}

export async function loadChampionNames(fetcher = fetch) {
  try {
    const names = await (await response('assets/champions.json', fetcher)).json();
    D.champNames = new Map(Object.values(names.data || {}).map(champion => [+champion.key, champion.name]));
  } catch { /* Numeric champion IDs remain accurate if optional name art is unavailable. */ }
}
