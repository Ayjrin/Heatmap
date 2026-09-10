import { D, S, buildDerived, playerIdentity } from './engine.mjs';

export class DatasetError extends Error {
  constructor(message, code = 'invalid') { super(message); this.code = code; }
}

async function response(url, fetcher = fetch) {
  const result = await fetcher(url, { cache: 'no-cache' });
  if (!result.ok) throw new DatasetError(result.status === 404
    ? 'No real dataset has been published yet.' : 'The dataset could not be loaded. Please retry.',
  result.status === 404 ? 'empty' : 'network');
  return result;
}

function views(buffer, part, rows) {
  if (buffer.byteLength !== part.bytes) throw new DatasetError('The dataset download is incomplete. Please retry.');
  const types = { u8: Uint8Array, i8: Int8Array, u16: Uint16Array, i16: Int16Array,
    u32: Uint32Array, i32: Int32Array };
  const cols = {};
  if (!Array.isArray(part.columns)) throw new DatasetError('The dataset schema is invalid.');
  for (const column of part.columns) {
    const type = types[column.type];
    if (!type || typeof column.name !== 'string' || Object.hasOwn(cols, column.name)
      || !Number.isSafeInteger(column.offset) || column.offset < 0 || column.length !== rows
      || column.offset % type.BYTES_PER_ELEMENT !== 0
      || column.offset + rows * type.BYTES_PER_ELEMENT > buffer.byteLength)
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
  'flags', 'team_gold_diff', 'victim_champ', 'victim_role', 'victim_player',
  'killer_champ', 'killer_role', 'killer_player'];

async function extendedFor(data, fetcher) {
  const part = data.manifest.extended;
  if (!part || !/^[\w.-]+$/.test(part.file)) throw new DatasetError('The advanced-filter dataset is missing.');
  const buffer = await (await response(`${data.base}/${part.file}`, fetcher)).arrayBuffer();
  const cols = views(buffer, part, data.rows);
  if (!cols.victim_gold_diff_lane || !cols.killer_gold_diff_lane)
    throw new DatasetError('The lane gold columns are missing.');
  return cols;
}

let extendedPromise = null;
export function loadExtended(fetcher = fetch) {
  if (D.extendedLoaded) return Promise.resolve();
  if (extendedPromise?.dataset === D.dataset_id) return extendedPromise.promise;
  const dataset = D.dataset_id, snapshot = { ...D };
  const holder = { dataset };
  holder.promise = extendedFor(snapshot, fetcher).then(cols => {
    if (D.dataset_id !== dataset) throw new DatasetError('The dataset changed. Please retry the filter.');
    Object.assign(D.cols, cols); D.extendedLoaded = true;
  }).finally(() => { if (extendedPromise === holder) extendedPromise = null; });
  extendedPromise = holder;
  return holder.promise;
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

/* Surrogate indices may change between releases; retain selections by identity. */
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
  const playerIndices = new Map(next.players.map((player, index) => [playerIdentity(player), index]));
  for (const group of ['subject', 'opponent']) {
    state[group].player = state[group].player.flatMap(entry => {
      const id = entry.id || playerIdentity(previous.players?.[entry.v]);
      const index = playerIndices.get(id);
      return index === undefined ? [] : [{ ...entry, id, v: index }];
    });
  }
  if (previous.dataset_id) {
    state.context.patch = remap(state.context.patch, previous.meta.patches || [], next.meta.patches || []);
    state.context.region = remap(state.context.region, previous.regions, next.regions, value => value.name);
  }
  const allowed = {
    role: new Set((next.meta.roles || []).map((_, i) => i)), champ: new Set(next.champions),
    player: new Set(next.players.map((_, i) => i)), side: new Set([100, 200]),
    region: new Set(next.regions.map((_, i) => i)),
    cause: new Set((next.meta.causes || []).map((_, i) => i)),
    patch: new Set((next.meta.patches || []).map((_, i) => i)),
  };
  for (const group of ['subject', 'opponent', 'context'])
    for (const facet of Object.keys(state[group]))
      state[group][facet] = state[group][facet].filter(entry => allowed[facet].has(entry.v));
}

/* Commit only a complete validated release; failures leave the previous map usable. */
export async function loadBundle(fetcher = fetch) {
  const pointer = validatePointer(await (await response('data/current.json', fetcher)).json());
  if (pointer.dataset_id === D.dataset_id) return false;
  const base = pointer.base;
  const manifest = await (await response(`${base}/manifest.json`, fetcher)).json();
  if (manifest.meta?.source_kind !== 'riot') throw new DatasetError('This dataset does not have verified Riot provenance.');
  if (manifest.meta.dataset_id && manifest.meta.dataset_id !== pointer.dataset_id)
    throw new DatasetError('The dataset version does not match its release.');
  if (!Number.isSafeInteger(manifest.rows) || manifest.rows < 0 || !manifest.core
    || !/^[\w.-]+$/.test(manifest.core.file)) throw new DatasetError('The real dataset is empty or invalid.');
  const [core, matches, players, champions, regions] = await Promise.all([
    response(`${base}/${manifest.core.file}`, fetcher).then(r => r.arrayBuffer()),
    ...['matches', 'players', 'champions', 'regions'].map(file =>
      response(`${base}/${file}.json`, fetcher).then(r => r.json())),
  ]);
  if (![matches, players, champions, regions].every(Array.isArray) || !matches.length)
    throw new DatasetError('The dataset metadata is incomplete.');
  if (new Set(matches.map(match => match.id)).size !== matches.length
    || players.some(player => !playerIdentity(player))
    || new Set(players.map(playerIdentity)).size !== players.length)
    throw new DatasetError('The dataset contains duplicate or missing identities.');
  const cols = views(core, manifest.core, manifest.rows);
  if (requiredCore.some(name => !cols[name])) throw new DatasetError('The dataset columns are incomplete.');
  for (let i = 0; i < manifest.rows; i++)
    if (cols.match_sk[i] >= matches.length || cols.region_sk[i] >= regions.length
      || cols.patch_sk[i] >= (manifest.meta.patches || []).length
      || cols.victim_player[i] >= players.length
      || (cols.cause[i] === 0 && cols.killer_player[i] >= players.length))
      throw new DatasetError('The dataset references invalid metadata.');
  const next = { cols, rows: manifest.rows, meta: manifest.meta, matches, players, champions, regions,
    manifest, base, dataset_id: pointer.dataset_id, extendedLoaded: false, champNames: D.champNames };
  if (S.lanegold) { Object.assign(next.cols, await extendedFor(next, fetcher)); next.extendedLoaded = true; }
  buildDerived(next);
  reconcileFilters(D, next);
  Object.assign(D, next, { lastResult: null, lastShown: null });
  extendedPromise = null;
  return true;
}

export async function loadChampionNames(fetcher = fetch) {
  try {
    const names = await (await response('assets/champions.json', fetcher)).json();
    D.champNames = new Map(Object.values(names.data || {}).map(champion => [+champion.key, champion.name]));
  } catch { /* Numeric champion IDs remain accurate if optional name art is unavailable. */ }
}
