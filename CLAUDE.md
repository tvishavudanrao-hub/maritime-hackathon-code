# CLAUDE.md - Tideline (Render app): what was done, how it fits together, what is left

Context for anyone (human or AI) picking this repo up. The user-facing docs are in `README.md`.
Python standard-library server (`server.py`) + a vanilla-JS single page (`index.html`, `app.js`). Deployed on Render. No keys are needed to run it.

## Work done on branch `feature/continuous-playback` (all pushed except where noted)

PR #1 is merged into `main` (playback, map zoom/pan, speed-ups, security). The later commits below need a new PR.

| Area | What | Where |
| --- | --- | --- |
| Playback | Continuous 0-200 h/s speed slider, `requestAnimationFrame` ship animation, zoom-aware speed (`10 h/s x 2.3 (zoomed out) = 23 h/s`) | `playback.js`, `app.js` (sim section) |
| Routes | Travelled = solid mint with dark edge, to go = dashed blue, legend | `app.js` (`drawVessels`), `index.html` |
| Live map | Dark Leaflet map (Esri dark-grey tiles, like the Streamlit Tideline), pans/zooms anywhere; SVG layers sit in a Leaflet pane; old SVG map is the fallback | `maplayer.js`, `app.js` (map engine), `index.html` |
| Render speed | Plan cache (same request = same answer for 10 min, click timestamps ignored), one plan at a time, reused baseline plan, start-up warm-up of the default plan, cheaper search loop, elapsed-time message | `plan_cache.py`, `server.py`, `optimizer.py` |
| News | Follows the route's zones and the fleet: tags "Your fleet / On your route / General", ranked | `news_rank.py` |
| Hazards | Zone score and per-reason sources on hover, port problem kinds (closed, shallow, unreachable, in zone, congested), collapsible legend | `optimizer._hazards`, `app.js`, `index.html` |
| Warnings | Condition-change banner, pulsing markers, timeline; de-duplicated, clears when resolved | `warnings.js` |
| Trade-off | Score shares (cost/carbon/risk/time, add to 100), "why it won" line, Pareto chart | `compare.js` |
| Incidents | Type-to-search incident box ranked by relevance to the route | `incidents.js` |
| AIS | Possible-stop / possible-queue / possibly-gone-dark alerts and a chokepoint traffic note, all advisory | `ais_anomaly.py`, `ais.py` |
| Security | `.gitignore` for `.env*`, `.venv`, keys; `.env.example`; `tools/secret_scan.py` and a pre-push hook | `tools/secret_scan.py` |

## NOT finished (uncommitted in the working tree when the session ended)

Los Angeles -> Rotterdam (the Americas network). The map can already pan there; the planner has no lanes yet.
- Done in the working tree, not committed: `data/network.json` (ports LAX and NYC, Atlantic/Caribbean/Panama/Pacific-coast/Cape Horn waypoints and lanes, zones `panama` and `cape_horn`, canal `panama`), `data/fleet.json` (sample `panama_toll_usd`), `optimizer.py` (corridors `panama` and `cape_horn`, Panama toll, corridor search generalised to any set of corridors), `tests/test_americas.py`.
- 16 of 17 `tests/test_americas.py` tests passed. The failing one needs `conditions.py` to return 4.5 m for latitude below -45 (Drake Passage sample wave) before the `lat < -30` branch. That edit was blocked by a hook, not applied.
- Still to do: apply that edit; run `python -m unittest tests.test_plan_speed tests.test_americas` (the golden tests prove the old routes are unchanged); add `['LAX','RTM','Los Angeles → Rotterdam']` and a New York lane to `LANES` in `app.js`; add `panama` and `cape_horn` to `news_rank.REGIONS` and `incidents.js` aliases; check the new lanes against land (they come from the Streamlit network, where they were land-checked); run the full suite; commit; push.
- No Pacific crossing exists, so Los Angeles <-> Asia is not routable yet.
- Adding more ports properly means running `tools/build_sealanes.py`, which needs the SeaRoute (MARNET) data from the Apache-2.0 `searoute-py` package and port positions from NGA's World Port Index. Both are downloads; ask the user first.

Dropped by the user: the weights, searchable-incident ports, and incident-knowledge-base phases beyond what is listed above.

## Run and test

```
python server.py                        # http://127.0.0.1:3001
python -m unittest discover -s tests    # Python tests (about 5 min; planner tests are slow)
node --test tests/*.test.js             # JS tests (also run by tests/test_playback_js.py)
python tools/secret_scan.py --install-hook   # once per clone
```

## Rules that matter here

- Honesty labels: ship positions are SIMULATED, scenarios are SIMULATED, live AIS is a sample and never confirmed, weather/news are advisory, data in `data/` is SAMPLE. Not for navigation.
- Keys (`AISSTREAM_API_KEY`, `OPENAI_API_KEY`) only as server environment variables (Render's Environment tab). Never in the repo, browser or logs.
- Push only to the feature branch, never to `main`. The pre-push hook scans the commits first. Commit as the GitHub no-reply identity; no personal emails or local paths in tracked files.
- Tests first for logic. Golden planner tests (`tests/test_plan_speed.py`) must stay green when the planner or network changes.
- Hooks in this environment ask for facts before the first edit of a file and before destructive commands; state them in text and retry. Edit CRLF files through tools that keep line endings.
- The built-in browser pane must be shown (`show_pane preview`) or animation frames are throttled.
- Known limits: the page loads Leaflet from unpkg and tiles from Esri (blocked network falls back to the schematic SVG map); a hidden tab pauses playback; Render's free CPU is far slower than a laptop, so the first toggle on a cold instance is still slow.
