#!/usr/bin/env node
/* Verify a published release with the production browser loader and engine.
 * Usage: node scripts/verify_bundle.mjs [web | path/to/release | current.json]
 */
import assert from 'node:assert/strict';
import { readFile, stat } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { D, S, defaults, scan, scanCells, layerField, ROLLUPS, UNKNOWN_TIER } from '../web/engine.mjs';
import { loadBundle, loadExtended, validatePointer, partitionsSettled, partitionFailures,
  ensurePartitions } from '../web/data.mjs';

const NULL_U32 = 4294967295;
const FILE = /^[\w.-]+(\/[\w.-]+){0,2}$/;

/* scan() and scanCells() must agree to the number; zones are compared in a
 * canonical order because the row scan lists them in encounter order. */
function comparable(result) {
  return { deaths: result.deaths, kills: result.kills, games: result.games, nD: result.nD, nK: result.nK,
    nMatch: result.nMatch, size: result.size, zones: [...result.zones].sort((a, b) => a.region - b.region) };
}

export async function verifyBundle(input = 'web') {
  const target = resolve(input);
  let pointer, release;
  const isFile = (await stat(target)).isFile();
  if (isFile) {
    pointer = validatePointer(JSON.parse(await readFile(target, 'utf8')));
    release = join(dirname(target), 'releases', pointer.dataset_id);
  } else {
    try {
      pointer = validatePointer(JSON.parse(await readFile(join(target, 'data/current.json'), 'utf8')));
      release = join(target, pointer.base);
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
      const manifest = JSON.parse(await readFile(join(target, 'manifest.json'), 'utf8'));
      pointer = validatePointer({ dataset_id: manifest.meta?.dataset_id,
        source_kind: manifest.meta?.source_kind, base: `data/releases/${manifest.meta?.dataset_id}` });
      release = target;
    }
  }
  const fetcher = async url => {
    if (url === 'data/current.json') return new Response(JSON.stringify(pointer));
    assert.ok(url.startsWith(`${pointer.base}/`), 'all bundle reads stay in the selected release');
    const filename = url.slice(pointer.base.length + 1);
    assert.ok(FILE.test(filename) && !filename.split('/').includes('..'), 'bundle paths stay inside the release');
    return new Response(await readFile(join(release, filename)));
  };
  Object.assign(S, defaults());
  delete D.dataset_id;
  await loadBundle(fetcher);
  await ensurePartitions(defaults());
  await partitionsSettled();
  assert.deepEqual(partitionFailures(), [], 'every partition loads');
  await loadExtended(fetcher);
  const parts = [...D.parts.values()];
  assert.ok(parts.every(part => part.loaded && part.extendedLoaded), 'every partition and its extended columns loaded');
  const tiers = D.tierNames.length;
  let championKills = 0, rows = 0, bytes = D.manifest.cells.bytes;
  for (const part of parts) {
    const c = part.cols;
    bytes += part.core.bytes + part.extended.bytes;
    const matches = JSON.parse(await readFile(join(release, part.matches_file), 'utf8'));
    assert.equal(matches.length, part.matches, `${part.key}: matches.json lists the partition's games`);
    assert.equal(new Set(matches.map(match => match.id)).size, matches.length, `${part.key}: unique match IDs`);
    assert.ok(matches.every(match => match.tier === part.tier && match.patch === part.patch), `${part.key}: matches carry the partition key`);
    for (let i = 0; i < part.rows; i++) {
      rows++;
      assert.ok(c.x[i] >= -120 && c.x[i] <= 14870 && c.y[i] >= -120 && c.y[i] <= 14980, `${part.key} row ${i}: map bounds`);
      assert.ok(c.victim_role[i] <= 4 || c.victim_role[i] === 255, `${part.key} row ${i}: victim role`);
      assert.ok(c.killer_role[i] <= 4 || c.killer_role[i] === 255, `${part.key} row ${i}: killer role`);
      assert.ok(c.victim_tier[i] < tiers || c.victim_tier[i] === UNKNOWN_TIER, `${part.key} row ${i}: victim tier`);
      assert.ok(c.killer_tier[i] < tiers || c.killer_tier[i] === UNKNOWN_TIER, `${part.key} row ${i}: killer tier`);
      assert.ok(c.victim_player[i] < D.players.count, `${part.key} row ${i}: victim player`);
      if (c.cause[i] === 0) {
        championKills++;
        assert.ok(c.killer_player[i] < D.players.count, `${part.key} row ${i}: killer player`);
      } else {
        assert.equal(c.killer_champ[i], 0, `${part.key} row ${i}: executions have no killer champion`);
        assert.equal(c.killer_player[i], NULL_U32, `${part.key} row ${i}: executions have no killer player`);
        assert.equal(c.killer_tier[i], UNKNOWN_TIER, `${part.key} row ${i}: executions have no killer rank`);
      }
    }
  }
  assert.equal(rows, D.manifest.rows, 'partition rows add up to the manifest');
  const games = new Set(parts.flatMap(part => Array.from(part.cols.match_sk, sk => part.matchBase + sk)));
  for (const size of ROLLUPS) {
    const state = defaults();
    const all = scan(size, D, state);
    assert.equal(all.nD, rows, 'every event contributes one death');
    assert.equal(all.nK, championKills, 'only champion kills enter the kill layer');
    assert.equal(all.nMatch, games.size, 'matching games count actual event matches');
    for (const layer of ['deaths', 'kills']) {
      state.layer = layer;
      const total = Array.from(layerField(all, state).out).reduce((sum, value) => sum + value, 0);
      assert.ok(Math.abs(total - ((layer === 'deaths' ? all.nD : all.nK) ? 1 : 0)) < 0.00001, `${layer} shares reconcile`);
    }
    // The Python-vs-JS contract: the prerendered cells must reproduce the row
    // scan exactly, for every rank and for the all-ranks default.
    const selections = [[], ...D.tiers.map(tier => [{ v: tier, neg: false }])];
    for (const selection of selections) {
      for (const layer of ['deaths', 'kills', 'danger']) {
        const cellState = { ...defaults(), layer, context: { ...defaults().context, tier: selection } };
        assert.deepEqual(comparable(scanCells(size, D, cellState)), comparable(scan(size, D, cellState)),
          `cells equal the row scan at ${size} for ${layer} / ranks ${JSON.stringify(selection.map(e => e.v))}`);
      }
    }
    // Test the actual actor predicates and ratio calculation, with executions
    // removed explicitly. This would fail if either actor branch lost rows.
    state.context.cause = [{ v: 0, neg: false }];
    state.layer = 'danger';
    const champions = scan(size, D, state), danger = layerField(champions, state).out;
    assert.deepEqual(champions.deaths, champions.kills, 'unfiltered champion events have equal actor counts');
    for (let i = 0; i < danger.length; i++) {
      if (champions.deaths[i] + champions.kills[i] >= 5) assert.equal(danger[i], 0.5, 'balanced cells have Danger 0.5');
      else assert.ok(Number.isNaN(danger[i]), 'sparse ratio cells stay suppressed');
    }
  }
  return { dataset_id: D.dataset_id, games: D.matchCount, events: rows, champion_kills: championKills,
    partitions: parts.length, tiers: D.tiers, players: D.players.count, bytes };
}

if (process.argv[1] && pathToFileURL(resolve(process.argv[1])).href === import.meta.url) {
  try { console.log('Bundle verified:', JSON.stringify(await verifyBundle(process.argv[2]))); }
  catch (error) { console.error(`Bundle verification failed: ${error.message}`); process.exitCode = 1; }
}
