import test, { beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { D, S, defaults, buildDerived, scan, layerField, zoneSummary, parseState, stateQuery } from '../web/engine.mjs';
import { loadBundle, loadExtended, validatePointer, reconcileFilters } from '../web/data.mjs';
import { CollectionController, runDescription } from '../web/collection.mjs';
import { verifyBundle } from '../scripts/verify_bundle.mjs';

// Synthetic inputs exist only in test memory or OS temp dirs. No test reads or
// writes the application's serving data directory.
function sample() {
  const u8 = (...v) => Uint8Array.of(...v), u16 = (...v) => Uint16Array.of(...v);
  const i16 = (...v) => Int16Array.of(...v), u32 = (...v) => Uint32Array.of(...v);
  const data = { rows: 6, extendedLoaded: true, meta: { roles: ['TOP', 'JUNGLE'],
    causes: ['CHAMPION', 'EXECUTION'], patches: ['16.17'] },
  matches: [{ id: 'NA1_1', duration: 1200 }, { id: 'NA1_2', duration: 1300 },
    { id: 'NA1_3', duration: 1400 }, { id: 'NA1_4', duration: 900 }],
  players: [{ id: 'puuid-a', name: 'Alpha' }, { id: 'puuid-b', name: 'Beta' }],
  champions: [10, 20], regions: [{ name: 'Other' }, { name: 'Top' }, { name: 'Mid' }],
  cols: {
    x: i16(1000, 1000, 1000, 1000, 1000, 9000), y: i16(2000, 2000, 2000, 2000, 2000, 9000),
    flags: u8(0, 0, 0, 1, 1, 0), second: u16(100, 120, 140, 160, 180, 200),
    team_gold_diff: i16(1000, 1000, 1000, -1000, -1000, 0), assists: u8(0, 1, 2, 1, 0, 0),
    region_sk: u8(1, 1, 1, 1, 1, 2), cause: u8(0, 0, 0, 0, 0, 1), patch_sk: u8(0, 0, 0, 0, 0, 0),
    victim_champ: u16(10, 10, 10, 20, 20, 10), killer_champ: u16(20, 20, 20, 10, 10, 0),
    victim_role: u8(0, 0, 0, 1, 1, 0), killer_role: u8(1, 1, 1, 0, 0, 255),
    victim_player: u16(0, 0, 0, 1, 1, 0), killer_player: u16(1, 1, 1, 0, 0, 65535),
    match_sk: u32(0, 0, 0, 1, 1, 2), victim_gold_diff_lane: i16(500, 500, 500, -500, -500, 100),
    killer_gold_diff_lane: i16(100, -500, -500, 500, 500, -32768),
  } };
  buildDerived(data); return data;
}

function release(id = 'test-v1', data = sample()) {
  const base = `data/releases/${id}`, files = new Map();
  const manifest = { version: 1, rows: data.rows, meta: { ...data.meta, dataset_id: id, source_kind: 'riot' } };
  for (const part of ['core', 'extended']) {
    const columns = [], pieces = []; let offset = 0;
    for (const [name, values] of Object.entries(data.cols)) {
      if (name.endsWith('_gold_diff_lane') !== (part === 'extended')) continue;
      const pad = (8 - offset % 8) % 8;
      pieces.push(Buffer.alloc(pad)); offset += pad;
      const type = { Uint8Array: 'u8', Uint16Array: 'u16', Int16Array: 'i16', Uint32Array: 'u32' }[values.constructor.name];
      columns.push({ name, type, offset, length: data.rows });
      pieces.push(Buffer.from(values.buffer, values.byteOffset, values.byteLength)); offset += values.byteLength;
    }
    manifest[part] = { file: `${part}.bin`, columns, bytes: offset };
    files.set(`${base}/${part}.bin`, Buffer.concat(pieces));
  }
  files.set('data/current.json', { dataset_id: id, base, source_kind: 'riot' });
  files.set(`${base}/manifest.json`, manifest);
  for (const key of ['matches', 'players', 'champions', 'regions']) files.set(`${base}/${key}.json`, data[key]);
  const calls = [];
  const fetcher = async url => {
    calls.push(url);
    if (!files.has(url)) return new Response('', { status: 404 });
    const value = files.get(url);
    return new Response(Buffer.isBuffer(value) ? value : JSON.stringify(value));
  };
  return { files, fetcher, calls, manifest, base };
}

beforeEach(() => {
  Object.assign(S, defaults());
  Object.assign(D, { cols: {}, rows: 0, meta: {}, matches: [], players: [], regions: [],
    champions: [], champNames: new Map(), extendedLoaded: false });
  delete D.dataset_id;
});

test('event layers count executions and matching games independently', () => {
  const data = sample(), state = defaults();
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
  const data = sample(), state = defaults();
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

test('lane gold applies to both matching actors and excludes the missing-value sentinel', () => {
  const data = sample(), state = defaults(); state.lanegold = [0, 1000];
  let result = scan(32, data, state);
  assert.deepEqual([result.nD, result.nK], [4, 3]);
  state.subject.champ = [{ v: 10, neg: false }];
  result = scan(32, data, state); assert.deepEqual([result.nD, result.nK], [4, 2]);
  data.extendedLoaded = false;
  assert.throws(() => scan(32, data, state), /still loading/);
});

test('team gold is blue minus red; context and inclusion/exclusion intersect', () => {
  const data = sample(), state = defaults(); state.gold = [1, 2000];
  assert.deepEqual([scan(32, data, state).nD, scan(32, data, state).nK], [3, 3]);
  state.time = [110, 150]; state.assists = [0, 1];
  assert.equal(scan(32, data, state).nD, 1);
  state.subject.champ = [{ v: 10, neg: false }, { v: 20, neg: false }, { v: 20, neg: true }];
  assert.deepEqual([scan(32, data, state).nD, scan(32, data, state).nK], [1, 0]);
});

test('zone summaries use exact original events across grid sizes', () => {
  const data = sample(), state = defaults(); state.subject.champ = [{ v: 10, neg: false }];
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

test('a cell counts the distinct games behind it, not its events', () => {
  // Five of the six events share one coordinate: three from game 0, two from
  // game 1. A cell of five events from two games has to say so, because the
  // colour alone cannot separate one game's massacre from many even trades.
  const data = sample(), state = defaults();
  for (const size of [32, 64, 128]) {
    const result = scan(size, data, state);
    const busy = data.cell[size][0], lone = data.cell[size][5];
    // Rows 0-4 are champion kills, so each one is both a death and a kill here.
    assert.deepEqual([result.deaths[busy], result.kills[busy]], [5, 5]);
    assert.equal(result.games[busy], 2);
    assert.equal(result.games[lone], 1);
    assert.equal(result.games.reduce((sum, n) => sum + n, 0), 3);
  }
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

test('player links preserve PUUID identity across reorder, duplicate names, and rename', () => {
  const first = { ...sample(), dataset_id: 'one' }, next = { ...sample(), dataset_id: 'two',
    players: [{ id: 'puuid-b', name: 'Same name' }, { id: 'puuid-a', name: 'Same name' }] };
  const state = defaults(); state.subject.player = [{ v: 0, neg: false }];
  state.opponent.player = [{ v: 1, neg: true }];
  const url = stateQuery(state, first);
  assert.match(url, /s.player_id=puuid-a/); assert.doesNotMatch(url, /s.player=0/);
  const restored = parseState(url); reconcileFilters({}, next, restored);
  assert.deepEqual(restored.subject.player, [{ v: 1, id: 'puuid-a', neg: false }]);
  assert.deepEqual(restored.opponent.player, [{ v: 0, id: 'puuid-b', neg: true }]);
  reconcileFilters(first, next, state); assert.equal(state.subject.player[0].v, 1);
  assert.deepEqual(parseState('?s.player=0').subject.player, []);
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

test('loader rejects absent, synthetic, and unknown provenance without replacing a live release', async () => {
  const live = release(); await loadBundle(live.fetcher); const original = D.cols;
  await assert.rejects(loadBundle(async () => new Response('', { status: 404 })), { code: 'empty' });
  for (const source_kind of ['fixture', 'synthetic', undefined]) {
    const bad = release('bad'); bad.files.get('data/current.json').source_kind = source_kind;
    await assert.rejects(loadBundle(bad.fetcher), /provenance/);
    assert.equal(D.cols, original);
  }
  const badManifest = release('bad-manifest'); badManifest.manifest.meta.source_kind = 'fixture';
  await assert.rejects(loadBundle(badManifest.fetcher), /provenance/);
  assert.throws(() => validatePointer({ dataset_id: '../escape', base: 'data/releases/../escape', source_kind: 'riot' }));
  assert.equal(D.dataset_id, 'test-v1');
});

test('zero-event games load as an empty heatmap with actual match metadata', async () => {
  const data = sample(); data.rows = 0;
  for (const [key, value] of Object.entries(data.cols)) data.cols[key] = new value.constructor(0);
  const empty = release('zero', data); await loadBundle(empty.fetcher);
  assert.equal(D.matches.length, 4); assert.equal(D.rows, 0);
  const result = scan(32); assert.deepEqual([result.nD, result.nK, result.nMatch], [0, 0, 0]);
  assert.equal(layerField(result).hi, 0); assert.deepEqual(zoneSummary(result), []);
});

test('incomplete downloads and invalid references cannot replace the previous release', async () => {
  await loadBundle(release().fetcher); const original = D.cols;
  const incomplete = release('incomplete'); incomplete.files.set(`${incomplete.base}/core.bin`, Buffer.alloc(0));
  await assert.rejects(loadBundle(incomplete.fetcher), /incomplete/);
  assert.equal(D.cols, original);
  const data = sample(); data.cols.victim_player[0] = 99;
  await assert.rejects(loadBundle(release('invalid-reference', data).fetcher), /invalid metadata/);
  assert.equal(D.cols, original);
  // Per-cell and per-zone game counts assume rows arrive grouped by match.
  // A release that broke that would inflate them silently, so it is refused.
  const ungrouped = sample(); ungrouped.cols.match_sk = Uint32Array.of(0, 1, 0, 1, 1, 2);
  await assert.rejects(loadBundle(release('ungrouped', ungrouped).fetcher), /not grouped by game/);
  assert.equal(D.cols, original);
});

test('URL lane-gold filtering loads extended data before committing the first dataset', async () => {
  Object.assign(S, parseState('?lanegold=0,1000&s.player_id=puuid-a'));
  const live = release(); await loadBundle(live.fetcher);
  assert.equal(D.extendedLoaded, true); assert.ok(live.calls.includes(`${live.base}/extended.bin`));
  assert.deepEqual([scan(32).nD, scan(32).nK], [4, 2]);
});

test('extended loading is lazy, deduplicates requests, and can retry a failure', async () => {
  const live = release(); await loadBundle(live.fetcher);
  assert.ok(!live.calls.some(url => url.endsWith('extended.bin')));
  await assert.rejects(loadExtended(async () => new Response('', { status: 503 })), /could not be loaded/);
  assert.equal(D.extendedLoaded, false);
  const a = loadExtended(live.fetcher), b = loadExtended(live.fetcher);
  assert.equal(a, b); await a;
  assert.equal(live.calls.filter(url => url.endsWith('extended.bin')).length, 1);
  assert.equal(D.extendedLoaded, true);
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

test('streamed download reports decoded byte progress, then actual loaded games', async () => {
  const live = release('streamed'), updates = [];
  const bytes = live.files.get(`${live.base}/core.bin`);
  const fetcher = url => url.endsWith('/core.bin') ? Promise.resolve(new Response(new ReadableStream({
    start(controller) {
      for (let i = 0; i < bytes.length; i += 23) controller.enqueue(bytes.subarray(i, i + 23));
      controller.close();
    },
  }), { headers: { 'content-length': '1', 'content-encoding': 'gzip' } })) : live.fetcher(url);
  await loadBundle(fetcher, update => updates.push(update));
  const downloads = updates.filter(update => update.stage === 'download');
  assert.ok(downloads.some(update => update.loaded > 0 && update.loaded < bytes.length));
  assert.ok(downloads.every(update => update.total === bytes.length && update.gamesLoaded === 0));
  assert.equal(downloads.at(-1).loaded, bytes.length);
  assert.deepEqual(updates.at(-1), { stage: 'complete', loaded: 4, total: 4, gamesLoaded: 4, totalGames: 4 });
  assert.ok(updates.some(update => update.stage === 'preparing'));
});

test('game validation yields between batches and counts zero-event games in the total', async () => {
  const data = sample(), rows = 60000;
  for (const [key, values] of Object.entries(data.cols)) {
    data.cols[key] = new values.constructor(rows).fill(values[0]);
  }
  data.rows = rows;
  for (let i = 0; i < rows; i++) data.cols.match_sk[i] = Math.floor(i / 20000);
  let painted = false;
  const updates = [];
  await loadBundle(release('batched', data).fetcher, update => {
    updates.push(update);
    if (update.stage === 'games' && update.gamesLoaded === 0) setTimeout(() => { painted = true; }, 0);
    if (update.stage === 'games' && update.gamesLoaded > 0) assert.equal(painted, true);
  });
  assert.deepEqual(updates.filter(update => update.stage === 'games').map(update => update.gamesLoaded), [0, 1, 2]);
  assert.equal(updates.at(-1).gamesLoaded, 4);
});

test('broken streams and oversized downloads never finish progress or replace the previous map', async () => {
  await loadBundle(release('previous').fetcher); const original = D.cols;
  for (const kind of ['truncated', 'oversized', 'broken']) {
    const live = release(kind), updates = [];
    const bytes = live.files.get(`${live.base}/core.bin`);
    const fetcher = url => url.endsWith('/core.bin') ? Promise.resolve(new Response(new ReadableStream({
      start(controller) {
        if (kind === 'broken') { controller.error(new Error('connection lost')); return; }
        controller.enqueue(kind === 'oversized' ? new Uint8Array(bytes.length + 1) : bytes.subarray(0, 10));
        controller.close();
      },
    }))) : live.fetcher(url);
    await assert.rejects(loadBundle(fetcher, update => updates.push(update)));
    assert.equal(D.cols, original);
    assert.ok(!updates.some(update => update.stage === 'complete'));
  }
});

test('collection preflight auth failure stays visible, and polling acknowledges a timed-out start', async () => {
  let method = 'fail'; const controller = new CollectionController({ uuid: () => 'same-id', fetcher: async () => {
    if (method === 'fail') throw new Error('Network failure');
    return new Response(JSON.stringify({ run: { run_id: 'same-id', status: 'auth_required', new_games: 0, target: 50,
      error: 'Update the Riot key.' }, dataset: { dataset_id: 'previous', count: 250 } }));
  } });
  await controller.start('smoke'); assert.ok(controller.pending);
  method = 'ok'; await controller.poll();
  assert.equal(controller.pending, null); assert.equal(controller.busy, false);
  assert.match(runDescription(controller.value.run), /Riot key needs updating/);
  assert.equal(controller.value.dataset.count, 250);
});

test('the run description names its stage and never quotes a game or kill count', () => {
  const run = { status: 'running', stage: 'discovering', new_games: 0, target: null,
    discovered_games: 12, players: 1000, scanned_players: 240 };
  assert.equal(runDescription(run), 'Collecting · discovering');
  assert.equal(runDescription({ ...run, stage: 'collecting' }), 'Collecting · collecting');
  assert.doesNotMatch(runDescription(run), /\d/);
  assert.equal(runDescription(null), 'Ready to collect ranked games.');
});

test('a rebuild is described by what it republishes, not by games it never collected', () => {
  const run = { mode: 'rebuild', status: 'running', stage: 'rebuilding', new_games: 0, target: null };
  // A rebuild collects nothing, so the collection labels read it as 'Collecting'
  // for work that is neither collecting nor idle.
  assert.match(runDescription(run), /Rebuilding the published dataset/);
  assert.match(runDescription({ ...run, status: 'succeeded' }), /Published dataset rebuilt/);
  assert.match(runDescription({ ...run, status: 'failed' }), /Dataset rebuild failed/);
  // Only the mode switches the wording; a real collection still reads as one.
  assert.equal(runDescription({ ...run, mode: 'full', stage: 'discovering' }),
    'Collecting · discovering');
});

test('production verifier reads only isolated test releases and rejects synthetic provenance', async () => {
  const root = await mkdtemp(join(tmpdir(), 'heatmap-browser-test-'));
  try {
    const live = release();
    for (const [name, value] of live.files) {
      const path = join(root, name); await mkdir(dirname(path), { recursive: true });
      await writeFile(path, Buffer.isBuffer(value) ? value : JSON.stringify(value));
    }
    const result = await verifyBundle(root);
    assert.deepEqual([result.games, result.events, result.champion_kills], [4, 6, 5]);
    await writeFile(join(root, 'data/current.json'), JSON.stringify({ ...live.files.get('data/current.json'), source_kind: 'fixture' }));
    await assert.rejects(verifyBundle(root), /provenance/);
  } finally { await rm(root, { recursive: true, force: true }); }
});
