import test, { beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { D, S, defaults, emptyDataset, buildDerived, scan, scanCells, cellsAnswer, layerField, zoneSummary,
  parseState, stateQuery, binIndex, ROLLUPS } from '../web/engine.mjs';
import { loadBundle, loadExtended, validatePointer, reconcileFilters, ensurePartitions, partitionsSettled,
  partitionFailures, partsLoaded, AUTO_LOAD_BYTES } from '../web/data.mjs';
import { CollectionController } from '../web/collection.mjs';
import { verifyBundle } from '../scripts/verify_bundle.mjs';

const NULL_U32 = 4294967295;
const REGIONS = [{ name: 'Other' }, { name: 'Top' }, { name: 'Mid' }];
const META = { roles: ['TOP', 'JUNGLE'], causes: ['CHAMPION', 'EXECUTION'], patches: ['16.17', '16.16'],
  tier_names: ['CHALLENGER', 'GRANDMASTER', 'MASTER', 'DIAMOND'] };

// Synthetic inputs exist only in test memory or OS temp dirs. No test reads or
// writes the application's serving data directory.
//
// Six events over four games. Games 0-1 are Diamond cohorts, game 2 is
// Challenger, game 3 (zero events) is Diamond; game 2 is on the second patch.
function sample() {
  const u8 = (...v) => Uint8Array.of(...v), u16 = (...v) => Uint16Array.of(...v);
  const i16 = (...v) => Int16Array.of(...v), u32 = (...v) => Uint32Array.of(...v);
  return {
    rows: 6,
    matches: [{ id: 'NA1_1', duration: 1200, tier: 3, patch: 0 }, { id: 'NA1_2', duration: 1300, tier: 3, patch: 0 },
      { id: 'NA1_3', duration: 1400, tier: 0, patch: 1 }, { id: 'NA1_4', duration: 900, tier: 3, patch: 0 }],
    players: [{ id: 'puuid-a', name: 'Alpha#NA1', tier: 3 }, { id: 'puuid-b', name: 'Beta#NA1', tier: 0 },
      { id: 'puuid-c', name: 'Gamma#NA1', tier: 255 }],
    champions: [10, 20],
    cols: {
      x: i16(1000, 1000, 1000, 1000, 1000, 9000), y: i16(2000, 2000, 2000, 2000, 2000, 9000),
      flags: u8(0, 0, 0, 1, 1, 0), second: u16(100, 120, 140, 160, 180, 200),
      team_gold_diff: i16(1000, 1000, 1000, -1000, -1000, 0), assists: u8(0, 1, 2, 1, 0, 0),
      region_sk: u8(1, 1, 1, 1, 1, 2), cause: u8(0, 0, 0, 0, 0, 1), patch_sk: u8(0, 0, 0, 0, 0, 1),
      victim_champ: u16(10, 10, 10, 20, 20, 10), killer_champ: u16(20, 20, 20, 10, 10, 0),
      victim_role: u8(0, 0, 0, 1, 1, 0), killer_role: u8(1, 1, 1, 0, 0, 255),
      victim_player: u32(0, 0, 0, 1, 1, 2), killer_player: u32(1, 1, 1, 0, 0, NULL_U32),
      victim_tier: u8(3, 3, 3, 0, 0, 255), killer_tier: u8(0, 0, 0, 3, 3, 255),
      match_sk: u32(0, 0, 0, 1, 1, 2), victim_gold_diff_lane: i16(500, 500, 500, -500, -500, 100),
      killer_gold_diff_lane: i16(100, -500, -500, 500, 500, -32768),
    },
  };
}

const TYPE = { Uint8Array: 'u8', Uint16Array: 'u16', Int16Array: 'i16', Uint32Array: 'u32' };
function pack(columns) {
  const descriptors = [], pieces = []; let offset = 0;
  for (const [name, values] of columns) {
    const pad = (8 - offset % 8) % 8;
    pieces.push(Buffer.alloc(pad)); offset += pad;
    descriptors.push({ name, type: TYPE[values.constructor.name], offset, length: values.length });
    pieces.push(Buffer.from(values.buffer, values.byteOffset, values.byteLength)); offset += values.byteLength;
  }
  return { columns: descriptors, bytes: offset, buffer: Buffer.concat(pieces) };
}

/* A v2 release: parts split by (tier, patch) with partition-local match keys,
 * cells computed with the production scan() over each tier, and players in
 * shards of two so the shard logic is exercised. */
function release(id = 'test-v1', data = sample(), { shardSize = 2 } = {}) {
  const base = `data/releases/${id}`, files = new Map();
  const partKeys = new Map();
  data.matches.forEach((match, i) => {
    const key = `${match.tier}-${META.patches[match.patch]}`;
    if (!partKeys.has(key)) partKeys.set(key, { key, tier: match.tier, patch: match.patch, matches: [], rowIndex: [] });
    partKeys.get(key).matches.push(i);
  });
  for (let i = 0; i < data.rows; i++) {
    const match = data.matches[data.cols.match_sk[i]];
    partKeys.get(`${match.tier}-${META.patches[match.patch]}`).rowIndex.push(i);
  }
  const parts = [], D2 = { parts: new Map(), cells: new Map() };
  let matchBase = 0;
  for (const part of partKeys.values()) {
    const cols = {};
    for (const [name, values] of Object.entries(data.cols)) {
      cols[name] = new values.constructor(part.rowIndex.map(i => name === 'match_sk'
        ? part.matches.indexOf(values[i]) : values[i]));
    }
    const core = pack(Object.entries(cols).filter(([name]) => !name.endsWith('_gold_diff_lane')));
    const extended = pack(Object.entries(cols).filter(([name]) => name.endsWith('_gold_diff_lane')));
    files.set(`${base}/parts/${part.key}/core.bin`, core.buffer);
    files.set(`${base}/parts/${part.key}/extended.bin`, extended.buffer);
    files.set(`${base}/parts/${part.key}/matches.json`, part.matches.map(i =>
      ({ id: data.matches[i].id, patch: part.patch, duration: data.matches[i].duration, blue_win: true, tier: part.tier })));
    parts.push({ key: part.key, tier: part.tier, patch: part.patch, rows: part.rowIndex.length, matches: part.matches.length,
      core: { file: `parts/${part.key}/core.bin`, bytes: core.bytes, columns: core.columns },
      extended: { file: `parts/${part.key}/extended.bin`, bytes: extended.bytes, columns: extended.columns },
      matches_file: `parts/${part.key}/matches.json` });
    const loaded = { key: part.key, tier: part.tier, patch: part.patch, rows: part.rowIndex.length, matches: part.matches.length,
      matchBase, cols, loaded: true, extendedLoaded: true };
    buildDerived(loaded); D2.parts.set(part.key, loaded); matchBase += part.matches.length;
  }
  // Cells per tier from the production row scan itself.
  const tiers = [...new Set(data.matches.map(match => match.tier))].sort((a, b) => a - b);
  const cellColumns = [], sections = [];
  for (const tier of tiers) {
    const state = { ...defaults(), context: { ...defaults().context, tier: [{ v: tier, neg: false }] } };
    const fine = scan(128, D2, state);
    const section = { tier, rows: fine.nD, kills: fine.nK, death_matches: fine.nMatch,
      kill_matches: scan(128, D2, { ...state, layer: 'kills' }).nMatch,
      matches: data.matches.filter(match => match.tier === tier).length };
    const zone = name => Uint32Array.from(REGIONS, (_, r) => fine.zones.find(z => z.region === r)?.[name] ?? 0);
    const columns = [[`deaths_128`, fine.deaths], [`kills_128`, fine.kills],
      ...ROLLUPS.map(size => [`games_${size}`, scan(size, D2, state).games]),
      ['zone_deaths', zone('deaths')], ['zone_kills', zone('kills')], ['zone_games', zone('games')]];
    sections.push({ section, columns });
  }
  const flat = sections.flatMap(({ columns }) => columns);
  const packed = pack(flat.map(([name, values], i) => [`${i}:${name}`, values]));
  let cursor = 0;
  for (const { section, columns } of sections) {
    section.columns = columns.map(([name]) => ({ ...packed.columns[cursor++], name }));
  }
  files.set(`${base}/cells.bin`, packed.buffer);
  // Players in PUUID order, sharded.
  const players = [...data.players].sort((a, b) => a.id < b.id ? -1 : 1);
  const shards = Math.ceil(players.length / shardSize), firstIds = [];
  for (let k = 0; k < shards; k++) {
    const slice = players.slice(k * shardSize, (k + 1) * shardSize);
    firstIds.push(slice[0].id);
    files.set(`${base}/players/names-${k}.json`, slice.map(player => player.name));
    files.set(`${base}/players/ids-${k}.json`, slice.map(player => player.id));
  }
  files.set(`${base}/players/index.json`, { count: players.length, shard_size: shardSize, first_id: firstIds });
  files.set(`${base}/players/tiers.bin`, Buffer.from(Uint8Array.from(players, player => player.tier)));
  const manifest = { version: 2, rows: data.rows, source_kind: 'riot',
    meta: { ...META, dataset_id: id, source_kind: 'riot', match_count: data.matches.length,
      max_duration: Math.max(...data.matches.map(match => match.duration)), tiers,
      unknown_tier_matches: data.matches.filter(match => match.tier === 255).length },
    cells: { file: 'cells.bin', bytes: packed.bytes, grid: 128, rollups: [128, 64, 32], regions: REGIONS.length,
      tiers: sections.map(({ section }) => section) },
    parts,
    players: { count: players.length, shard_size: shardSize, shards, index: 'players/index.json',
      tiers: { file: 'players/tiers.bin', bytes: players.length } } };
  files.set('data/current.json', { dataset_id: id, base, source_kind: 'riot' });
  files.set(`${base}/manifest.json`, manifest);
  files.set(`${base}/regions.json`, REGIONS);
  files.set(`${base}/champions.json`, data.champions);
  const calls = [];
  const fetcher = async url => {
    calls.push(url);
    if (!files.has(url)) return new Response('', { status: 404 });
    const value = files.get(url);
    return new Response(Buffer.isBuffer(value) ? value : JSON.stringify(value));
  };
  // Dictionary indices follow PUUID order, so map the sample's players to it.
  const sk = Object.fromEntries(players.map((player, i) => [player.id, i]));
  return { files, fetcher, calls, manifest, base, data: D2, sk, players };
}

/* Every test sample loads with the same assertions the live app uses. */
async function load(live, ...rest) {
  const changed = await loadBundle(live.fetcher, ...rest);
  await partitionsSettled();
  return changed;
}

beforeEach(() => {
  Object.assign(S, defaults());
  Object.assign(D, emptyDataset());
  delete D.dataset_id;
});

test('event layers count executions and matching games independently', () => {
  const { data } = release(), state = defaults();
  let result = scan(32, data, state);
  assert.deepEqual([result.nD, result.nK, result.nMatch], [6, 5, 3]);
  state.layer = 'kills'; assert.equal(scan(32, data, state).nMatch, 2);
  state.subject.champ = [{ v: 10, neg: false }];
  result = scan(32, data, state);
  assert.deepEqual([result.nD, result.nK, result.nMatch], [4, 2, 1]);
  state.layer = 'deaths'; assert.equal(scan(32, data, state).nMatch, 2);
  state.layer = 'danger'; assert.equal(scan(32, data, state).nMatch, 3);
  state.opponent.role = [{ v: 1, neg: false }];
  assert.deepEqual([scan(32, data, state).nD, scan(32, data, state).nK], [3, 2]);
});

test('events bin at their own normalized coordinates, with no side folding', () => {
  const { data } = release(), state = defaults();
  const result = scan(32, data, state);
  // Rows 0-4 sit at normalized (0.075, 0.140) whatever side the victim is on;
  // red is never folded onto blue, because the Rift is not a true mirror.
  const shared = 4 * 32 + 2, far = 19 * 32 + 19;
  assert.equal(result.deaths[shared], 5); assert.equal(result.kills[shared], 5);
  assert.equal(result.deaths[far], 1);
  assert.equal(result.deaths.reduce((a, b) => a + b, 0), result.nD);
  state.subject.side = [{ v: 200, neg: false }];
  const red = scan(32, data, state);
  assert.deepEqual([red.nD, red.nK], [2, 3]);
  assert.equal(red.deaths[shared], 2); assert.equal(red.kills[shared], 3);
});

test('rank filters: context skips partitions, actor ranks filter rows, unknown rank matches executions', () => {
  const { data } = release(), state = defaults();
  state.context.tier = [{ v: 3, neg: false }];
  assert.deepEqual([scan(32, data, state).nD, scan(32, data, state).nK, scan(32, data, state).nMatch], [5, 5, 2]);
  state.context.tier = [{ v: 3, neg: true }];
  assert.deepEqual([scan(32, data, state).nD, scan(32, data, state).nMatch], [1, 1]);
  state.context.tier = [];
  state.subject.tier = [{ v: 0, neg: false }];          // Challenger subjects: victims 3-4, killers 0-2
  assert.deepEqual([scan(32, data, state).nD, scan(32, data, state).nK], [2, 3]);
  state.subject.tier = [];
  state.opponent.tier = [{ v: 255, neg: false }];        // no killer at all: the execution only
  assert.deepEqual([scan(32, data, state).nD, scan(32, data, state).nK], [1, 0]);
  state.opponent.tier = [{ v: 255, neg: true }];
  assert.deepEqual([scan(32, data, state).nD, scan(32, data, state).nK], [5, 5]);
});

test('the prerendered cells reproduce the row scan for every rank selection, layer and grid', async () => {
  const live = release(); await load(live);
  const selections = [[], [{ v: 3, neg: false }], [{ v: 0, neg: false }], [{ v: 0, neg: true }], [{ v: 3, neg: true }, { v: 0, neg: true }]];
  const canonical = r => ({ deaths: r.deaths, kills: r.kills, games: r.games, nD: r.nD, nK: r.nK, nMatch: r.nMatch,
    zones: [...r.zones].sort((a, b) => a.region - b.region) });
  for (const tier of selections) for (const layer of ['deaths', 'kills', 'danger', 'opportunity']) for (const size of ROLLUPS) {
    const state = { ...defaults(), layer, context: { ...defaults().context, tier } };
    assert.ok(cellsAnswer(state));
    assert.deepEqual(canonical(scanCells(size, D, state)), canonical(scan(size, D, state)), `${layer} ${size} ${JSON.stringify(tier)}`);
  }
  const state = defaults(); state.subject.champ = [{ v: 10, neg: false }];
  assert.equal(cellsAnswer(state), false);
  state.subject.champ = []; state.context.patch = [{ v: 0, neg: false }];
  assert.equal(cellsAnswer(state), false, 'patch selections need the rows');
});

test('lane gold applies to both matching actors and excludes the missing-value sentinel', () => {
  const { data } = release(), state = defaults(); state.lanegold = [0, 1000];
  let result = scan(32, data, state);
  assert.deepEqual([result.nD, result.nK], [4, 3]);
  state.subject.champ = [{ v: 10, neg: false }];
  result = scan(32, data, state); assert.deepEqual([result.nD, result.nK], [4, 2]);
  data.parts.get('3-16.17').extendedLoaded = false;
  assert.throws(() => scan(32, data, state), /still loading/);
});

test('team gold is blue minus red; context and inclusion/exclusion intersect', () => {
  const { data } = release(), state = defaults(); state.gold = [1, 2000];
  assert.deepEqual([scan(32, data, state).nD, scan(32, data, state).nK], [3, 3]);
  state.time = [110, 150]; state.assists = [0, 1];
  assert.equal(scan(32, data, state).nD, 1);
  state.subject.champ = [{ v: 10, neg: false }, { v: 20, neg: false }, { v: 20, neg: true }];
  assert.deepEqual([scan(32, data, state).nD, scan(32, data, state).nK], [1, 0]);
});

test('zone summaries use exact original events across grid sizes', () => {
  const { data } = release(), state = defaults(); state.subject.champ = [{ v: 10, neg: false }];
  // Region 1 holds five events from two games; region 2 holds one from a third.
  const expected = [{ region: 1, deaths: 3, kills: 2, games: 2 },
    { region: 2, deaths: 1, kills: 0, games: 1 }];
  for (const size of [32, 64, 128]) {
    const result = scan(size, data, state);
    assert.deepEqual(result.zones, expected);
    assert.equal(zoneSummary(result, 'deaths')[0].value, 0.75);
    assert.equal(zoneSummary(result, 'danger')[0].value, 8 / 15);
  }
});

test('a cell counts the distinct games behind it, not its events, across partitions', () => {
  // Five of the six events share one coordinate: three from game 0, two from
  // game 1. A cell of five events from two games has to say so, because the
  // colour alone cannot separate one game's massacre from many even trades.
  const { data } = release(), state = defaults();
  const part = data.parts.get('3-16.17'), lone = data.parts.get('0-16.16');
  for (const size of [32, 64, 128]) {
    const result = scan(size, data, state);
    const busy = part.cell[size][0], far = lone.cell[size][0];
    assert.deepEqual([result.deaths[busy], result.kills[busy]], [5, 5]);
    assert.equal(result.games[busy], 2);
    assert.equal(result.games[far], 1);
    assert.equal(result.games.reduce((sum, n) => sum + n, 0), 3);
  }
  // Partition-local match keys never collide: both parts start at match_sk 0.
  assert.equal(part.cols.match_sk[0], lone.cols.match_sk[0]);
});

test('Danger uses the prior, suppresses sparse cells, and complements Opportunity', () => {
  const result = { size: 2, deaths: Uint32Array.of(4, 1, 0, 0), kills: Uint32Array.of(1, 0, 5, 0), nD: 5, nK: 6 };
  const state = defaults(); state.layer = 'danger'; const danger = layerField(result, state).out;
  assert.ok(Math.abs(danger[0] - 9 / 15) < 1e-7);
  assert.ok(Number.isNaN(danger[1])); assert.ok(Number.isNaN(danger[3]));
  assert.ok(Math.abs(danger[2] - 5 / 15) < 1e-7);
  state.layer = 'opportunity'; const opportunity = layerField(result, state).out;
  assert.ok(Math.abs(danger[0] + opportunity[0] - 1) < 1e-7);
});

test('player links preserve PUUID identity and load exactly one ids shard, then one names shard for the label', async () => {
  Object.assign(S, parseState('?s.player_id=puuid-c&o.exclude_player_id=puuid-a'));
  const live = release(); await load(live);
  assert.deepEqual(S.subject.player, [{ v: live.sk['puuid-c'], id: 'puuid-c', neg: false }]);
  assert.deepEqual(S.opponent.player, [{ v: live.sk['puuid-a'], id: 'puuid-a', neg: true }]);
  const idShards = live.calls.filter(url => /players\/ids-\d+\.json$/.test(url));
  assert.deepEqual(idShards.sort(), [`${live.base}/players/ids-0.json`, `${live.base}/players/ids-1.json`]);
  assert.ok(!live.calls.some(url => url.includes('players/names-')), 'names are not fetched until shown');
  assert.equal(D.players.nameOf(live.sk['puuid-c']), null);
  await D.players.ensureNames(D.players.shardOf(live.sk['puuid-c']));
  assert.equal(D.players.nameOf(live.sk['puuid-c']), 'Gamma#NA1');
  assert.equal(live.calls.filter(url => url.includes('players/names-')).length, 1);
  assert.equal(D.players.tierOf(live.sk['puuid-a']), 3);
  const url = stateQuery(S, D);
  assert.match(url, /s\.player_id=puuid-c/); assert.match(url, /o\.exclude_player_id=puuid-a/);
  assert.equal(await D.players.resolve('puuid-zzz'), -1);
  assert.equal(await D.players.resolve('aaa'), -1);
  assert.deepEqual(parseState('?s.player=0').subject.player, []);
});

test('rank ordinals survive the URL round trip and reconcile against the release', () => {
  const state = parseState('?c.tier=3,!255&s.tier=0&o.tier=!255');
  assert.deepEqual(state.context.tier, [{ v: 3, neg: false }, { v: 255, neg: true }]);
  assert.deepEqual(state.subject.tier, [{ v: 0, neg: false }]);
  assert.deepEqual(state.opponent.tier, [{ v: 255, neg: true }]);
  const url = stateQuery(state, D);
  assert.match(url, /c\.tier=3%2C%21255/); assert.match(url, /s\.tier=0/); assert.match(url, /o\.tier=%21255/);
  const next = { ...emptyDataset(), meta: META, regions: REGIONS, champions: [10, 20], tiers: [0, 3],
    tierNames: META.tier_names, players: { byId: new Map(), valid: () => false } };
  state.context.tier.push({ v: 2, neg: false });   // Master: no such cohort in this release
  state.subject.tier.push({ v: 9, neg: false });   // beyond the rank table
  reconcileFilters({}, next, state);
  assert.deepEqual(state.context.tier, [{ v: 3, neg: false }]);
  assert.deepEqual(state.subject.tier, [{ v: 0, neg: false }]);
  assert.deepEqual(state.opponent.tier, [{ v: 255, neg: true }]);
});

test('URL validation retains supported controls and discards malformed values', () => {
  const state = parseState('?layer=danger&grid=12&scale=log&time=300,100&gold=-99999,99999&lanegold=-500,1000&mirror=1&s.champ=10,!20,bad');
  // mirror was removed, not renamed: a stale link must not revive the control.
  assert.ok(!('mirror' in state));
  assert.equal(state.layer, 'danger'); assert.equal(state.grid, 'auto'); assert.equal(state.scale, 'log');
  assert.equal(state.time, null); assert.deepEqual(state.gold, [-25000, 25000]);
  assert.deepEqual(state.lanegold, [-500, 1000]);
  assert.deepEqual(state.subject.champ, [{ v: 10, neg: false }, { v: 20, neg: true }]);
});

test('loader rejects absent, synthetic, unsupported and unknown provenance without replacing a live release', async () => {
  const live = release(); await load(live); const original = D.parts;
  await assert.rejects(loadBundle(async () => new Response('', { status: 404 })), { code: 'empty' });
  for (const source_kind of ['fixture', 'synthetic', undefined]) {
    const bad = release('bad'); bad.files.get('data/current.json').source_kind = source_kind;
    await assert.rejects(loadBundle(bad.fetcher), /provenance/);
    assert.equal(D.parts, original);
  }
  const badManifest = release('bad-manifest'); badManifest.manifest.meta.source_kind = 'fixture';
  await assert.rejects(loadBundle(badManifest.fetcher), /provenance/);
  const old = release('old-format'); old.manifest.version = 1;
  await assert.rejects(loadBundle(old.fetcher), { code: 'unsupported' });
  const escape = release('escape'); escape.manifest.parts[0].core.file = 'parts/../../secret.bin';
  await assert.rejects(loadBundle(escape.fetcher), /partitions are invalid/);
  assert.throws(() => validatePointer({ dataset_id: '../escape', base: 'data/releases/../escape', source_kind: 'riot' }));
  assert.equal(D.dataset_id, 'test-v1');
});

test('the map is ready from the cells before any partition, then partitions merge smallest first', async () => {
  const live = release(), updates = [], merged = [];
  const changed = await loadBundle(live.fetcher, update => updates.push(update), part => merged.push(part.key));
  assert.equal(changed, true);
  const ready = updates.findIndex(update => update.stage === 'ready');
  assert.ok(ready > 0 && updates.slice(0, ready).every(update => ['metadata', 'cells'].includes(update.stage)));
  assert.ok(!live.calls.slice(0, live.calls.findIndex(url => url.endsWith('cells.bin')) + 1).some(url => url.includes('/parts/')),
    'no partition is requested before the cells');
  assert.deepEqual(partsLoaded(), { loaded: 0, total: 2, rows: 0 });
  const result = scanCells(32);
  assert.deepEqual([result.nD, result.nK, result.nMatch], [6, 5, 3]);
  assert.deepEqual([scan(32).nD, scan(32).nMatch], [0, 0], 'the row scan is empty until parts land');
  await partitionsSettled();
  assert.deepEqual(merged, ['0-16.16', '3-16.17'], 'smaller partitions first');
  assert.deepEqual(partsLoaded(), { loaded: 2, total: 2, rows: 6 });
  assert.deepEqual(updates.at(-1).parts, { loaded: 2, total: 2, rows: 6 });
  assert.deepEqual([scan(32).nD, scan(32).nK, scan(32).nMatch], [6, 5, 3]);
  assert.ok(!live.calls.some(url => url.endsWith('extended.bin')), 'extended columns stay lazy');
  assert.ok(!live.calls.some(url => url.endsWith('matches.json')), 'the page never needs per-partition match lists');
});

test('a corrupt partition is reported without evicting the others, and can be retried', async () => {
  const live = release('corrupt');
  live.files.set(`${live.base}/parts/3-16.17/core.bin`, Buffer.alloc(3));
  const updates = [];
  await loadBundle(live.fetcher, update => updates.push(update));
  await partitionsSettled();
  assert.deepEqual(partitionFailures(), ['3-16.17']);
  assert.ok(updates.some(update => update.stage === 'partition-failed' && update.part === '3-16.17'));
  assert.deepEqual([scan(32).nD, scan(32).nMatch], [1, 1], 'the intact partition is usable');
  assert.deepEqual([scanCells(32).nD, scanCells(32).nMatch], [6, 3], 'the cells still answer the default view');
  live.files.set(`${live.base}/parts/3-16.17/core.bin`, release('fixed').files.get(`${live.base.replace('corrupt', 'fixed')}/parts/3-16.17/core.bin`));
  await ensurePartitions(defaults());
  await partitionsSettled();
  assert.deepEqual(partitionFailures(), []);
  assert.equal(scan(32).nD, 6);
});

test('invalid references and ungrouped rows fail a partition, never the release', async () => {
  for (const [name, mutate, message] of [
    ['invalid-reference', data => { data.cols.victim_player[0] = 99; }, /invalid metadata/],
    ['bad-tier', data => { data.cols.killer_tier[0] = 200; }, /invalid metadata/],
    ['ungrouped', data => { data.cols.match_sk = Uint32Array.of(0, 1, 0, 1, 1, 2); }, /not grouped by game/],
  ]) {
    Object.assign(D, emptyDataset()); delete D.dataset_id;
    const data = sample(); mutate(data);
    const live = release(name, data), updates = [];
    // Per-cell and per-zone game counts assume rows arrive grouped by match.
    // A release that broke that would inflate them silently, so it is refused.
    await loadBundle(live.fetcher, update => updates.push(update));
    await partitionsSettled();
    assert.equal(D.dataset_id, name);
    assert.ok(partitionFailures().includes('3-16.17'), name);
    assert.ok(!D.parts.get('3-16.17').loaded);
    assert.match(String(D.loading.failed.get('3-16.17').message), message);
  }
});

test('zero-event partitions load without a download and games still count', async () => {
  const data = sample(); data.rows = 0;
  for (const [key, value] of Object.entries(data.cols)) data.cols[key] = new value.constructor(0);
  const empty = release('zero', data); await load(empty);
  assert.equal(D.matchCount, 4); assert.deepEqual(partsLoaded(), { loaded: 2, total: 2, rows: 0 });
  assert.ok(!empty.calls.some(url => url.endsWith('core.bin')), 'empty parts are not fetched');
  const result = scan(32); assert.deepEqual([result.nD, result.nK, result.nMatch], [0, 0, 0]);
  assert.equal(layerField(result).hi, 0); assert.deepEqual(zoneSummary(result), []);
  assert.deepEqual([scanCells(32).nD, scanCells(32).nMatch], [0, 0]);
});

test('a single-tier release exposes one cohort and one cells section', async () => {
  const data = sample(); for (const match of data.matches) match.tier = 0;
  const live = release('single', data); await load(live);
  assert.deepEqual(D.tiers, [0]); assert.deepEqual([...D.cells.keys()], [0]);
  assert.deepEqual([...D.parts.keys()].sort(), ['0-16.16', '0-16.17']);
  assert.deepEqual([scanCells(32).nD, scan(32).nD], [6, 6]);
});

test('a dataset change while partitions stream discards the late merges', async () => {
  const first = release('first'), merged = [];
  let release_ = null;
  const gate = new Promise(resolve => { release_ = resolve; });
  const slow = async url => { if (url.includes('/parts/')) await gate; return first.fetcher(url); };
  await loadBundle(slow, null, part => merged.push(part.key));
  const settled = partitionsSettled();
  const second = release('second'); await load(second);
  release_(); await settled;
  assert.equal(D.dataset_id, 'second');
  assert.deepEqual(merged.filter(key => !D.parts.get(key)?.loaded), []);
  assert.ok([...D.parts.values()].every(part => part.loaded && part.cols.match_sk.length === part.rows));
  assert.deepEqual([scan(32).nD, scan(32).nMatch], [6, 3]);
});

test('URL lane-gold filtering loads extended columns for every partition, eagerly while set', async () => {
  Object.assign(S, parseState('?lanegold=0,1000&s.player_id=puuid-a'));
  const live = release(); await load(live);
  assert.ok([...D.parts.values()].every(part => part.extendedLoaded));
  assert.equal(live.calls.filter(url => url.endsWith('extended.bin')).length, 2);
  // Alpha is the victim of rows 0-2 (lane gold +500) and the killer of rows
  // 3-4 (+500); the execution in row 5 belongs to Gamma.
  assert.deepEqual([scan(32).nD, scan(32).nK], [3, 2]);
});

test('extended loading is lazy, per partition, deduplicates requests, and can retry a failure', async () => {
  const live = release(); await load(live);
  assert.ok(!live.calls.some(url => url.endsWith('extended.bin')));
  await assert.rejects(loadExtended(async () => new Response('', { status: 503 })), /could not be loaded/);
  assert.ok([...D.parts.values()].every(part => !part.extendedLoaded));
  const a = loadExtended(live.fetcher), b = loadExtended(live.fetcher);
  await Promise.all([a, b]);
  assert.equal(live.calls.filter(url => url.endsWith('extended.bin')).length, 2);
  assert.ok([...D.parts.values()].every(part => part.extendedLoaded));
  S.lanegold = [0, 1000];
  assert.deepEqual([scan(32).nD, scan(32).nK], [4, 3]);
});

test('oversized partitions wait for a filter that needs them; the cells cover the default view', async () => {
  const live = release('big'), updates = [];
  const big = live.manifest.parts.find(part => part.key === '3-16.17');
  big.core.bytes = AUTO_LOAD_BYTES + 1;   // claims to be too large to auto-load
  S.context.tier = [{ v: 0, neg: false }];
  await loadBundle(live.fetcher, update => updates.push(update));
  await partitionsSettled();
  assert.ok(D.parts.get('0-16.16').loaded && !D.parts.get('3-16.17').loaded);
  assert.equal(partitionFailures().length, 0);
  S.context.tier = [];
  assert.deepEqual(partsLoaded(), { loaded: 1, total: 2, rows: 1 });
  assert.equal(scanCells(32).nD, 6);
  // Asking for it fetches it; the size lie then fails validation, which is
  // reported as a partition failure and leaves the loaded partition alone.
  await ensurePartitions(S);
  await partitionsSettled();
  assert.deepEqual(partitionFailures(), ['3-16.17']);
  assert.ok(live.calls.some(url => url.endsWith('parts/3-16.17/core.bin')));
  assert.equal(scan(32).nD, 1);
});

test('streamed partition download reports decoded byte progress', async () => {
  const live = release('streamed'), updates = [];
  const bytes = live.files.get(`${live.base}/parts/3-16.17/core.bin`);
  const fetcher = url => url.endsWith('parts/3-16.17/core.bin') ? Promise.resolve(new Response(new ReadableStream({
    start(controller) {
      for (let i = 0; i < bytes.length; i += 23) controller.enqueue(bytes.subarray(i, i + 23));
      controller.close();
    },
  }), { headers: { 'content-length': '1', 'content-encoding': 'gzip' } })) : live.fetcher(url);
  await loadBundle(fetcher, update => updates.push(update));
  await partitionsSettled();
  const downloads = updates.filter(update => update.stage === 'partition' && update.part === '3-16.17' && !update.merged);
  assert.ok(downloads.some(update => update.loaded > 0 && update.loaded < bytes.length));
  assert.ok(downloads.every(update => update.total === bytes.length));
  assert.equal(downloads.at(-1).loaded, bytes.length);
  assert.ok(updates.some(update => update.stage === 'partition' && update.merged && update.part === '3-16.17'));
});

test('row validation yields between batches so progress can paint', async () => {
  const data = sample(), rows = 60000;
  for (const [key, values] of Object.entries(data.cols)) data.cols[key] = new values.constructor(rows).fill(values[0]);
  data.rows = rows;
  for (let i = 0; i < rows; i++) data.cols.match_sk[i] = i < 20000 ? 0 : i < 40000 ? 1 : 3;
  data.cols.patch_sk.fill(0);
  data.matches[2].tier = 3; data.matches[2].patch = 0;   // one four-game Diamond partition
  let painted = false, merged = false;
  await loadBundle(release('batched', data).fetcher, update => {
    if (update.stage === 'partition' && update.merged) { merged = true; assert.equal(painted, true); }
  });
  setTimeout(() => { painted = true; }, 0);
  await partitionsSettled();
  assert.equal(merged, true);
  assert.equal(scan(32).nMatch, 3);
});

test('broken streams and oversized downloads fail the partition and never the previous map', async () => {
  await load(release('previous')); const original = D.parts;
  for (const kind of ['truncated', 'oversized', 'broken']) {
    const live = release(kind);
    const bytes = live.files.get(`${live.base}/parts/3-16.17/core.bin`);
    const fetcher = url => url.endsWith('parts/3-16.17/core.bin') ? Promise.resolve(new Response(new ReadableStream({
      start(controller) {
        if (kind === 'broken') { controller.error(new Error('connection lost')); return; }
        controller.enqueue(kind === 'oversized' ? new Uint8Array(bytes.length + 1) : bytes.subarray(0, 10));
        controller.close();
      },
    }))) : live.fetcher(url);
    await loadBundle(fetcher);
    await partitionsSettled();
    assert.deepEqual(partitionFailures(), ['3-16.17'], kind);
    assert.ok(D.parts.get('0-16.16').loaded);
  }
  assert.notEqual(D.parts, original);
  // A whole-release failure (cells) keeps the previous release.
  const before = D.dataset_id;
  const cellsBroken = release('cells-broken'); cellsBroken.files.set(`${cellsBroken.base}/cells.bin`, Buffer.alloc(0));
  await assert.rejects(loadBundle(cellsBroken.fetcher), /incomplete/);
  assert.equal(D.dataset_id, before);
});

test('collection retry reuses a UUID; a new click after completion gets a fresh UUID', async () => {
  const requests = []; let attempt = 0, uuid = 0;
  const controller = new CollectionController({ uuid: () => `request-${++uuid}`, fetcher: async (url, options) => {
    assert.equal(url, '/api/runs'); const body = JSON.parse(options.body); requests.push(body);
    if (++attempt === 1) throw new Error('Network failure');
    return new Response(JSON.stringify({ run: { run_id: body.requestId, status: 'starting' }, dataset: null }), { status: 202 });
  } });
  await controller.start('smoke'); await controller.start('small');
  assert.equal(requests.length, 1, 'a pending retry cannot silently change its mode');
  await controller.start('smoke'); assert.deepEqual(requests[0], requests[1]);
  await controller.start('smoke'); assert.equal(requests.length, 2, 'active runs disable starts');
  controller.accept({ run: { run_id: 'request-1', status: 'succeeded' }, dataset: null });
  await controller.start('small'); assert.deepEqual(requests[2], { mode: 'small', requestId: 'request-2' });
});

test('default collection fetch keeps the browser-global receiver', async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = function () {
    assert.equal(this, globalThis);
    return Promise.resolve(new Response(JSON.stringify({ run: null, dataset: null })));
  };
  try {
    const controller = new CollectionController();
    await controller.poll();
    assert.equal(controller.message, '');
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('a visit checks status before starting full collection, and completion does not restart it', async () => {
  const calls = []; let run = null;
  const controller = new CollectionController({ uuid: () => 'visit-1', fetcher: async (url, options) => {
    calls.push(url);
    if (url === '/api/runs') {
      assert.deepEqual(JSON.parse(options.body), { mode: 'full', requestId: 'visit-1' });
      run = { run_id: 'visit-1', status: 'starting' };
    }
    return Response.json({ run, dataset: null });
  } });
  await Promise.all([controller.visit(), controller.visit()]);
  assert.deepEqual(calls, ['/api/status', '/api/runs']);
  run.status = 'succeeded'; await controller.visit(); await controller.visit();
  assert.equal(calls.filter(url => url === '/api/runs').length, 1);
});

test('visits leave active runs alone even after they finish', async () => {
  for (const status of ['starting', 'running']) {
    let run = { run_id: 'other-visitor', status };
    const controller = new CollectionController({ fetcher: async url => {
      assert.equal(url, '/api/status'); return Response.json({ run });
    } });
    await controller.visit();
    run = { ...run, status: 'succeeded' }; await controller.visit();
    assert.equal(controller.visitComplete, true);
  }
});

test('failed or malformed status never authorizes a launch; a later successful check can', async () => {
  for (const payload of [undefined, {}, { run: { run_id: 'unknown', status: 'unknown' } }]) {
    let healthy = false, starts = 0;
    const controller = new CollectionController({ uuid: () => 'visit', fetcher: async url => {
      if (url === '/api/runs') { starts++; return Response.json({ run: null }); }
      if (healthy) return Response.json({ run: null });
      if (!payload) throw new Error('offline');
      return Response.json(payload);
    } });
    await controller.visit(); assert.equal(starts, 0);
    healthy = true; await controller.visit(); assert.equal(starts, 1);
  }
});

test('automatic launch retries reuse their UUID and stop on an expired key', async () => {
  const requests = []; let run = null;
  const controller = new CollectionController({ uuid: () => 'same-visit', fetcher: async (url, options) => {
    if (url === '/api/runs') {
      requests.push(JSON.parse(options.body));
      if (requests.length === 1) throw new Error('response lost');
      run = { run_id: 'same-visit', status: 'auth_required', error: 'Riot key needs updating' };
    }
    return Response.json({ run });
  } });
  await controller.visit(); assert.ok(controller.pending);
  await controller.visit(); await controller.visit();
  assert.equal(requests.length, 2); assert.deepEqual(requests[0], requests[1]);
  assert.equal(controller.value.run.status, 'auth_required');
  assert.equal(controller.pending, null);
});

test('status can acknowledge a lost launch response without another POST', async () => {
  let run = null, starts = 0;
  const controller = new CollectionController({ uuid: () => 'lost-response', fetcher: async url => {
    if (url === '/api/runs') {
      starts++; run = { run_id: 'lost-response', status: 'succeeded' };
      throw new Error('connection lost after completion');
    }
    return Response.json({ run });
  } });
  await controller.visit(); await controller.visit(); await controller.visit();
  assert.equal(starts, 1); assert.equal(controller.pending, null);
});

test('collection preflight auth failure is retained, and polling acknowledges a timed-out start', async () => {
  let method = 'fail'; const controller = new CollectionController({ uuid: () => 'same-id', fetcher: async () => {
    if (method === 'fail') throw new Error('Network failure');
    return new Response(JSON.stringify({ run: { run_id: 'same-id', status: 'auth_required', new_games: 0, target: 50,
      error: 'Update the Riot key.' }, dataset: { dataset_id: 'previous', count: 250 } }));
  } });
  await controller.start('smoke'); assert.ok(controller.pending);
  method = 'ok'; await controller.poll();
  assert.equal(controller.pending, null); assert.equal(controller.busy, false);
  assert.equal(controller.value.run.status, 'auth_required');
  assert.equal(controller.value.dataset.count, 250);
});

test('production verifier reads only isolated test releases, checks the cells contract, and rejects synthetic provenance', async () => {
  const root = await mkdtemp(join(tmpdir(), 'heatmap-browser-test-'));
  try {
    const live = release();
    for (const [name, value] of live.files) {
      const path = join(root, name); await mkdir(dirname(path), { recursive: true });
      await writeFile(path, Buffer.isBuffer(value) ? value : JSON.stringify(value));
    }
    const result = await verifyBundle(root);
    assert.deepEqual([result.games, result.events, result.champion_kills, result.partitions, result.players], [4, 6, 5, 2, 3]);
    assert.deepEqual(result.tiers, [0, 3]);
    // Break the Python-vs-JS contract: a cells section that disagrees with its rows.
    const manifest = live.files.get(`${live.base}/manifest.json`);
    const cells = Buffer.from(live.files.get(`${live.base}/cells.bin`));
    const column = manifest.cells.tiers[0].columns.find(c => c.name === 'games_32');
    cells.writeUInt32LE(cells.readUInt32LE(column.offset + 4 * binIndex(9000, 9000, 32)) + 1, column.offset + 4 * binIndex(9000, 9000, 32));
    await writeFile(join(root, live.base, 'cells.bin'), cells);
    await assert.rejects(verifyBundle(root), /cells equal the row scan/);
    await writeFile(join(root, 'data/current.json'), JSON.stringify({ ...live.files.get('data/current.json'), source_kind: 'fixture' }));
    await assert.rejects(verifyBundle(root), /provenance/);
  } finally { await rm(root, { recursive: true, force: true }); }
});
