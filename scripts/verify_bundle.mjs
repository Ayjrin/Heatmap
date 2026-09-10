#!/usr/bin/env node
/* Verify a published release with the production browser loader and engine.
 * Usage: node scripts/verify_bundle.mjs [web | path/to/release | current.json]
 */
import assert from 'node:assert/strict';
import { readFile, stat } from 'node:fs/promises';
import { dirname, basename, join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { D, S, defaults, scan, layerField, ROLLUPS, playerIdentity } from '../web/engine.mjs';
import { loadBundle, loadExtended, validatePointer } from '../web/data.mjs';

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
    assert.equal(basename(filename), filename, 'bundle filenames cannot traverse directories');
    return new Response(await readFile(join(release, filename)));
  };
  Object.assign(S, defaults());
  delete D.dataset_id;
  await loadBundle(fetcher);
  await loadExtended(fetcher);
  const c = D.cols;
  assert.equal(new Set(D.matches.map(match => match.id)).size, D.matches.length, 'unique match IDs');
  assert.equal(new Set(D.players.map(playerIdentity)).size, D.players.length, 'unique player IDs');
  let championKills = 0;
  for (let i = 0; i < D.rows; i++) {
    assert.ok(c.x[i] >= -120 && c.x[i] <= 14870 && c.y[i] >= -120 && c.y[i] <= 14980, `row ${i}: map bounds`);
    assert.ok(c.victim_role[i] <= 4 || c.victim_role[i] === 255, `row ${i}: victim role`);
    assert.ok(c.killer_role[i] <= 4 || c.killer_role[i] === 255, `row ${i}: killer role`);
    if (c.cause[i] === 0) championKills++;
    else assert.equal(c.killer_champ[i], 0, `row ${i}: executions have no killer champion`);
  }
  for (const size of ROLLUPS) {
    const state = defaults();
    const all = scan(size, D, state);
    assert.equal(all.nD, D.rows, 'every event contributes one death');
    assert.equal(all.nK, championKills, 'only champion kills enter the kill layer');
    assert.equal(all.nMatch, new Set(c.match_sk).size, 'matching games count actual event matches');
    for (const layer of ['deaths', 'kills']) {
      state.layer = layer;
      const total = Array.from(layerField(all, state).out).reduce((sum, value) => sum + value, 0);
      assert.ok(Math.abs(total - ((layer === 'deaths' ? all.nD : all.nK) ? 1 : 0)) < 0.00001, `${layer} shares reconcile`);
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
  return { dataset_id: D.dataset_id, games: D.matches.length, events: D.rows,
    champion_kills: championKills, bytes: D.manifest.core.bytes + D.manifest.extended.bytes };
}

if (process.argv[1] && pathToFileURL(resolve(process.argv[1])).href === import.meta.url) {
  try { console.log('Bundle verified:', JSON.stringify(await verifyBundle(process.argv[2]))); }
  catch (error) { console.error(`Bundle verification failed: ${error.message}`); process.exitCode = 1; }
}
