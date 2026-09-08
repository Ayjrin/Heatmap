# ProLeague Heatmap

Where NA Challenger and Grandmaster players **win and lose fights** on Summoner's Rift —
a filterable kill/death heatmap built from the Riot API.

**This is not professional play.** Official esports data (LCK/LEC/LTA) is not exposed by the
Riot API at all; only public-shard accounts are. What this analyses is **apex solo queue**.
The name says "ProLeague"; the data says Challenger + Grandmaster ladder.

Not endorsed by Riot Games.

---

## Quickstart — no API key needed

```bash
make demo
```

Generates synthetic Riot-shaped payloads, runs the full transform, verifies the output
bundle, and serves the app at <http://localhost:8000>.

Only the crawl needs a key. Everything downstream runs on fixtures, which is what makes the
whole pipeline testable offline.

```bash
make test        # 27 pytest checks + the node bundle verifier
make discover    # phases A+B only (~22 min, needs a key) — returns U
make crawl       # the full crawl (needs a key)
make build       # bronze -> silver -> browser bundle
```

> **Environment note.** Every target exports `PYTHONDONTWRITEBYTECODE=1`. On this machine,
> letting Python write `__pycache__` into the working tree stalls the process for minutes —
> it blocks in `_Py_read` at 0% CPU. With the variable set, the same imports take ~60 ms.
> Harmless everywhere else; every entry point here is short-lived.

## The four layers

One fact table, one scan, four renderings — all normalized relative to the rest of the map.

| Layer | Value per cell | Reads as |
|---|---|---|
| **Deaths** | `D / ΣD` | Where this cohort dies |
| **Kills** | `K / ΣK` | Where this cohort kills |
| **Danger** | `D / (D + K)` | If a fight happens here, do they lose it? |
| **Opportunity** | `1 − Danger` | If a fight happens here, do they win it? |

Danger and Opportunity are exact complements — the same number, two palettes. Both ship
because flipping between them is what makes contested ground pop.

### The thing that makes the ratio work

A `CHAMPION_KILL` is one death **and** one kill at the same coordinate. Count all ten
players and `D == K` in every cell, so Danger is 0.5 everywhere and the map is uniformly
grey. The ratio only carries signal when the filters select a **subset of players**.

So the UI splits filters into **Subject** (whose kills and deaths we plot), **Opponent**
(who they fought) and **Context** (the moment). The Subject is a player subset by
construction, which designs the degeneracy out instead of warning about it. Switching layers
doesn't change the filters — it changes which side of the row the Subject matches against:

```
Deaths  ->  Subject matches the VICTIM
Kills   ->  Subject matches the KILLER
Danger  ->  both, divided
```

*"Kills, by supports, from three named players, who weren't playing Pyke"* is one filter set;
flipping the layer shows where those same players **died** instead. One definition, four maps.

This property is asserted in both test suites — `test_danger_degeneracy_with_no_subject_filter`
and the node verifier's *"Danger is 0.5 in all N champion-kill cells"*.

## What was cut, and why

Cutting well is most of the design. Each of these was checked against real API behaviour.

| Cut | Reason |
|---|---|
| **All ward / vision features** | `WARD_PLACED` and `WARD_KILL` carry no `position`. Riot has declined to add it since 2019 ([dev-rel #160](https://github.com/RiotGames/developer-relations/issues/160)) — deliberately, since published ward coordinates would show exactly where opponents are blind. Without position *and* without a reliable lifetime, "did the team have a ward" is a map-wide boolean that is true ~95% of the time after minute three. Dropped entirely rather than shipped as a proxy that looks like vision data and is not. |
| **Exposure normalization** (`deaths / player-minutes`) | Superseded by Danger, which is self-normalizing from the same rows. Deletes the whole occupancy pipeline: no position-sample extraction, no fountain exclusion, no respawn modelling in the denominator, no inter-frame path interpolation. |
| **Objective / building heatmaps** | Weak standalone. The events stay in bronze, so this is reversible without re-crawling. |
| **Killing ability** | Narrow, and it widens every row for a column most queries never touch. |

## Architecture

```
league-v4 ladder ─┐
                  ├─> bronze (gzipped raw JSON, never re-fetched)
match-v5 ─────────┘        │
                           ├─ transform ─> silver (Parquet)
                           └─ build     ─> core.bin + extended.bin + sidecars
                                              │
                                        static site (Canvas 2D)
```

**ELT, not ETL, and the rate limit is the whole argument.** Re-extracting costs ~18 hours
against a 3,000 req/hr ceiling; re-deriving from bronze costs minutes. Every schema change is
a re-transform, never a re-crawl. The ETL counterfactual saves ~5 GB of S3 — about 12 cents a
month — and costs the ability to ever change your mind.

**The two halves have opposite parallelism**, which is why they get different compute:
the extract is a serial singleton (one key is one budget — ten workers give one worker's
throughput plus a coordination problem); the transform is embarrassingly parallel per match.

**The crawl target is not a chosen number.** 24 h of dev key allows 35,499 matches, but only
~20–27k are discoverable from ~1,000 apex players, so the answer is "all of them" (~18.3 h).
Phase B costs ~1,000 calls and 22 minutes and returns the exact figure *before* a single
match call is spent — run `make discover` first.

### The wire format is not Arrow

Every column is a fixed-width scalar with a sentinel null: no strings, no validity bitmaps,
no nesting. Arrow's file format exists to carry exactly what we don't have, and Arrow-JS
costs ~150 KB gzipped. Instead: one `.bin` of 8-byte-aligned column buffers plus a JSON
manifest, consumed with zero copies —

```js
const col = new Int16Array(buffer, offset, rowCount);
```

`scripts/verify_bundle.mjs` asserts the Python writer and the JS reader agree, including
that every offset aligns for its typed-array view.

**54 bytes/row**, split into `core.bin` (everything the default view and headline filters
need) and `extended.bin` (lazy-loaded on first use of an advanced filter). ~2 MB of timeline
JSON collapses to ~28 rows ≈ 1.5 KB — about 1,400:1.

## Verified

```
27 passed in 0.11s        pytest
all checks passed         node scripts/verify_bundle.mjs
```

Covers: canonicalization is an involution and the naive raw-unit mirror drifts >100 units
(the box is 14990 × 15100, not square); Y flips exactly once; the economy join is
backward-only, so it can never read the frame that already contains the killer's kill gold;
executions (`killerId == 0`) carry no killer block and never enter the Kills layer; both
actor blocks are independently populated; `gameDuration` is handled in both unit
conventions; every rejection rule fires; all values fit their columns.

**Not verified:** anything requiring a live API key. The crawl path is written and
key-ready but has never made a real request from this machine.

## Known limitations

1. **Apex solo queue, not pro play.** Stated above, repeated here because it matters.
2. **`seed_tier` means "a Challenger/GM player was in this match"** — not that all ten were.
   Rank at match time does not exist in the API.
3. **Frame-derived state is up to 60 s stale.** Gold and CS come from the last frame at or
   before the event. Level does not — it is replayed from `LEVEL_UP` events, so it is exact.
4. **Respawn timers and objective spawn timers are modelled**, not returned by the API. They
   gate a count and a bitmask, not a measurement.
5. **~0.9% of ranked games have a blank `teamPosition`.** Those rows are flagged
   (`position_imputed`) and their lane-relative diffs nulled, not silently imputed.
6. **PUUIDs are encrypted per API key** and do not join across keys. The warehouse is keyed
   on `matchId` + `participantId`; only Riot IDs ship to the browser.
7. **Region polygons are hand-authored** to the Rift's known layout, not surveyed from game
   files. They label and roll up; nothing numeric depends on their exact edges.

## Layout

```
PROJECT_PLAN.html          the full design doc — read this first
config.yaml                unknown keys are a hard error
src/proleague/
  extract/                 routing types, rate limiter, client, bronze, state
  transform/               geometry, regions, kills (the star transform)
  serve/bundle.py          the browser wire format
scripts/
  crawl.py                 phases A-D, resumable
  build_dataset.py         bronze -> silver -> gold
  make_fixture.py          synthetic payloads, so no key is needed
  verify_bundle.mjs        asserts the Python writer and JS reader agree
web/                       index.html + app.js (engine) + ui.js (controls)
tests/                     27 checks
deploy.sh                  S3 + CloudFront, versioned immutable data paths
```

Design rationale, the cost model (~$0.47 first month, $0.12/mo steady state) and the full
Riot API reference are in **`PROJECT_PLAN.html`**.
# Heatmap
