# Tideline — Maritime Route Intelligence
Try here:
**(https://maritime-hackathon-code.onrender.com/#overview)**
**Hackathon project:** c0mpiled-14: UC Berkeley (October 17, 2026); YC Fall 2026 "New Operating Systems for the Physical World" RFS.

> **One sentence:** A decision-support dashboard that compares maritime trade corridors using modeled ship economics, forecast marine conditions, and headline-based risk signals, then proposes diversions when routes become infeasible.

[Tideline dashboard]
<img width="1468" height="769" alt="image" src="https://github.com/user-attachments/assets/6ad2060d-f5e5-4893-82f0-5be9d4cadb8a" />


## Run it locally (no installs, no paid API keys)

Requires **Python 3.9+** and internet access for optional live feeds. No pip, npm, or database required.

### Windows PowerShell

```powershell
cd path\to\pelagic-maritime-mvp
py -3 server.py
```

Or double-click `run-windows.bat` from the extracted project folder.

### macOS / Linux

```bash
cd pelagic-maritime-mvp
python3 server.py
```

Then open **http://127.0.0.1:3001** in a browser. The webserver listens on **all interfaces by default** (`HOST=0.0.0.0`, which hosts such as Render need). On a shared network set `HOST=127.0.0.1` to keep it on your machine. Stop with `Ctrl+C`.

For another local port: `PORT=4000 python3 server.py` (PowerShell: `$env:PORT=4000; py -3 server.py`).

### Keeping secrets out of git

API keys (`AISSTREAM_API_KEY`, `OPENAI_API_KEY`) are read only by the server, from environment variables, and never sent to the browser. Set them in your shell locally, or in the Environment tab of the Render service. `.env.example` lists every variable with placeholders; copy it for your own notes but never commit the copy. `.gitignore` already excludes `.env*` (except the example), `.venv/`, key and certificate files, `runtime/` and similar.

Run `python tools/secret_scan.py --install-hook` once after cloning. It installs a git pre-push hook that scans the commits you are about to push for key-like strings and for files that must never be committed, and stops the push if it finds any (it prints where, never the secret itself). You can also run `python tools/secret_scan.py` (tracked files) or `--history` (every commit) by hand. If a real key was ever committed, revoke it at the provider: deleting it from the repo does not make it safe again.

### Running on a small host (for example Render)

Planning is CPU-bound Python, so a shared or throttled CPU can be 10 to 30 times slower than a laptop. The server keeps that manageable:

- Identical plan requests share one answer for 10 minutes (the click time of a scenario does not count). Anyone who asks while it is being computed waits for it instead of starting another.
- Only one plan runs at a time, so several viewers do not slow each other to a crawl.
- The plan "as if nothing had happened" is computed once per ship and route and reused for every scenario toggle.
- When the server starts, the page's opening plan is computed once the live weather arrives, so the first visitor does not pay for it.

Settings (all optional): `TIDELINE_PLAN_CACHE_S` (seconds to reuse a plan, default 600), `TIDELINE_PLAN_SLOTS` (plans at once, default 1), `TIDELINE_PREWARM=0` (skip the start-up plan). A cached answer carries `"cached": true`. Plans are model estimates and use the live weather and headlines as of when they were computed, so they can be up to the cache time old.

## The hackathon demo (about 2 minutes)

1. **Today's world.** Singapore → Rotterdam, MV Aurora, World conditions = Today. Tideline recommends the **Cape**, as most carriers currently route, and the **Data sources** panel shows why: published fuel prices, war-risk premiums, Red Sea security, live PortWatch traffic. Each input shows its date and source.
2. **Normal times.** Flip to Normal times: Suez wins again. Toggle **Red Sea passage closed** and it diverts to the Cape, with the original plan shown as a red dashed line and the extra days, cost and fuel named against the event.
3. **Mid-voyage.** Reset the closure. Slide **Voyage progress** to about day 9: the ship is in the Arabian Sea and Tideline says **Stay on course**. Now close the Red Sea: **Change course now**. Tideline turns the ship back toward Colombo and around the Cape from where it actually is, with the fuel it actually has, and says why.
4. **Ask Tideline.** Click "Paste a sample news report" and send it. The AI extracts a threat (not a closure), a storm and port congestion, tags each with its source, re-plans, and every figure in the reply is checked against the planner.
5. **Honest limits.** Jebel Ali → Rotterdam with Hormuz closed: no sea route exists, and Tideline names Fujairah instead of inventing a detour.

## Implemented MVP

| Component | What it really does | What it does **not** claim |
| --- | --- | --- |
| Map | Embedded Natural Earth low-resolution land shapes, two manually curated waypoint paths per trade lane | Certified navigable tracks / ECDIS charts |
| Routes | Singapore, Shanghai, Jebel Ali → Rotterdam; Suez vs Cape alternatives | Any-origin any-destination navigation |
| ETA | Sums great-circle waypoint-to-waypoint nautical miles, divides by vessel speed, adds forecast-sensitive wave delay within available horizon and estimated Suez allowance | Actual arrival prediction with port and vessel constraints |
| Cost | Modeled fuel = days × 50 tonnes/day × (speed/18 knots)^3 × $560/tonne; plus $22k/day opex and user-defined canal fee | Audited commercial quote or official canal bill |
| Weather | Server fetches Open-Meteo Marine hourly **wave_height** at route samples; matches samples to estimated arrival hours, never forecasts beyond seven days | Weather model for the entire ~20–35 day voyage |
| Geopolitics | Server fetches recent articles via GDELT or Google News RSS; matching disruption keywords contribute **capped, non-authoritative** risk penalties | Verified alerts, closures, or AIS-derived incidents |
| Optimization | Scores two pre-defined alternatives using objective-specific weighted cost/time/risk; simulated **hard closures** disqualify affected routes | Vessel autopilot or dynamic shortest paths through all sea lanes |
| Ship marker | Illustrative 40%-along-route position | Live vessel tracking / AIS |

**A critical modeling point:** A ship departing from a Persian Gulf port cannot bypass the Strait of Hormuz entirely *by sea* if that strait is blocked. The model correctly reports no feasible departure; an alternate transport plan (such as inland movement to a port outside the Gulf) would need to be evaluated separately.

## Live data and licensing

- [Open-Meteo Marine](https://open-meteo.com/en/docs/marine-weather-api): model-generated, hourly wave-height forecasts at sample waypoints; keyless non-commercial access, subject to Open-Meteo terms, provider uptime, and attribution. Forecast horizon is **7 days** in this implementation. Its own documentation warns it is unsuitable for coastal navigation.
- [GDELT DOC 2.0 API](https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/): recent press headlines. [Google News RSS](https://news.google.com/) fallback. These sources are journalistic signals, **not** verified warnings. The UI never interprets headline keywords as hard closures.
- [UKMTO incidents](https://www.ukmto.org/recent-incidents): authoritative security advisories to integrate in a production iteration, *not connected in this MVP*.
- [Suez Canal Authority toll schedule](https://www.suezcanal.gov.eg/English/Navigation/Tolls/Pages/TollsTable.aspx): official canal dues vary with size, category, and rules. **The default $350k figure is an illustrative user-editable assumption, not a current published quote.**
- [MarineTraffic API reference](https://servicedocs.marinetraffic.com/): AIS integration is possible with separately authorized data access; **not connected**.
- [Natural Earth](https://www.naturalearthdata.com/about/terms-of-use/): low-resolution public-domain land geometry embedded as SVG; used only as a geographic backdrop.

The API caches successful **and failed** requests for 15 minutes by default to avoid stressing free services. Feeds may be unavailable on a disconnected network; that never silently turns into fake "live" values.

## Engine details

For each alternative \(r\), derive:

- `distance_nm` = sum of haversine distances between the human-curated waypoint pairs.
- `sailing_days` = `distance_nm / (speed_knots × 24)`.
- `voyage_days` = `sailing_days + wave_delay_days + canal_wait_days + simulated_storm_delay_days`.
- `fuel_tonnes_per_day` = `50 × (speed_knots/18)^3` (simple illustrative cubic speed-fuel scaling).
- `operating_cost_usd` = `fuel_burn × $560 + voyage_days × $22,000 + canal_fee`.
- `risk_proxy` = transparent fixed corridor/chokepoint factors + capped headline keyword penalties + available forecast wave penalties + simulated weather penalties.

The planner *ranks* candidates with a weighted combination of cost, duration, and risk; this is **not** a statistical or calibrated loss model. "Fastest", "Lowest cost", "Lowest risk", and "Balanced" shift the weights. Hard route closures remove infeasible candidates before ranking.

Why not predict beyond seven days? Because a multiweek voyage exceeds the sampled forecast window. The app keeps the unforecast remainder at baseline vessel speed and prominently notes the uncertainty. Do **not** treat the final ETA as operationally validated.

## Planning backend (new)

The server now includes a voyage optimizer behind four endpoints. The existing dashboard is unchanged; wiring it to these endpoints is the next step.

| Endpoint | What it returns |
| --- | --- |
| `GET /api/fleet` | Five **sample** vessel profiles and their speed options |
| `GET /api/network` | Waypoint network, ports (fuel price, fees, waits, draft limits), risk zones, presets, event types |
| `GET /api/fuel-model` | Active fuel model and its learned coefficients per ship |
| `POST /api/plan` | Recommended plan, best alternative per corridor, cost breakdown, checkpoints with fuel levels, bunkering, and, when events are supplied, the original plan and what changed |

Example request:

```json
{"ship_id": "aurora", "origin": "SIN", "destination": "RTM", "priority": "balanced",
 "deadline_days": 35, "deadline_type": "hard", "fuel_onboard_t": 2500,
 "events": [{"type": "zone_closure", "zone": "bab", "source": "Sample advisory", "timestamp": "2026-10-16T08:00:00Z"}]}
```

Event types: `zone_closure`, `port_closure`, `security` (raises risk and war-risk premium), `storm` (center, radius_nm, hs_m, start_h, end_h), `traffic` (PortWatch ratio), `news` (headline hits), `fuel_price`, `port_congestion`. Every event keeps its source and timestamp, and the response marks each as affecting the voyage or not.

**How it works**

- **Fuel model** (`fuel_model.py`): Admiralty cubic law with wave, wind and current effects, plus a per-ship least-squares regression on log fuel per mile. The regression is trained on **simulated** data (physics plus 7% noise) and recovers the physics: speed exponent about 2.0, displacement exponent about 0.67, hold-out error about 1%. Set `TIDELINE_FUEL_MODEL=physics` to use the formula directly.
- **Risk model** (`risk_model.py`): transparent scores, not trained. Weather (S-curve against the ship's wave limit), security (zone baseline plus capped news and event bumps), traffic shift; combined per leg with noisy-OR and over the route by time exposed.
- **Optimizer** (`optimizer.py`): label-setting search over routes x speed per segment x refueling. Hard rules remove plans (closures, avoid lists, draft, tank capacity, fuel reserve, hard deadlines, waves above the ship's limit). The rest are ranked by cash cost plus preset-weighted risk, time and carbon.
- **Conditions** (`conditions.py`): **sample** seasonal wave heights and the Agulhas current, overridden by storm events. Live Open-Meteo forecasts plug in here next.

When no plan exists, the response says why: a closure that seals the origin (with the nearest open port beyond it), a port draft limit, a storm above the ship's limit, or the fastest possible time when a deadline is too tight.

## Interface (Tideline)

A light fleet-operations layout with a left sidebar:
- **Overview** — vessels on the map coloured by status (on track, attention, decision needed, in port), KPI cards, and *Decisions needed* cards from monitoring alerts (plus risk-watch cards from today's conditions).
- **Vessels** — the fleet with status, voyage, ETA, draft, tank, war cover and fuel-model state.
- **Voyages** — the planner: inputs on the left; map, route alternatives and the voyage plan (plan, costs, compliance) on the right.
- **Operations** — approve and monitor voyages, dispatch drafts, audit log.
- **Compliance** — insurance policies, OFAC sanctions screening of the fleet, official NGA warnings.
- **Reports** — data sources behind every number, news and sea conditions, audit log.
- **Settings** — live status of every connection (AI, weather, PortWatch, AIS, sanctions, warnings, monitoring, audit chain).
- **Ask Tideline** (bottom right) opens the AI assistant; the search box jumps to a vessel, port or voyage; **Demo view** keeps the one-click pitch buttons.

## Operations: approve, monitor, dispatch, audit (new)

The **Operations** panel (below the voyage plan) turns a recommendation into a managed voyage.

- **Approve & monitor.** Enter your name (required) and an optional note and ship MMSI. The server re-runs the optimizer itself (it never trusts a plan sent by the browser), screens the vessel against the OFAC sanctions list, stores the voyage in `runtime/voyages.json`, and logs who approved what.
- **Monitoring.** A background thread re-checks every active voyage every 15 minutes (`TIDELINE_MONITOR_MIN`). The ship's position comes from its linked AIS transponder when available, else from the latest noon report, else from the approved schedule. Tideline re-plans from that position with the latest weather and conditions, compares *stay on course* with *change course*, and raises alerts: change course, arrival drift over 12 h, fuel on arrival down over 20%, no feasible plan, ship off the approved route.
- **Accept re-plan.** One click makes the re-plan the new committed route (revision 2, 3, ...); the old one is kept in the history.
- **Operator inputs.** Report an event (closure, threat, storm, berth queue), send a noon report (fuel on board, optional position), link an AIS MMSI, or use **+24 h (demo clock)** to move a voyage forward in time on stage. The demo clock is labeled as simulated everywhere.
- **Dispatch drafts.** *Orders & bunker enquiry* generates voyage orders for the master (route, speeds, ETAs, bunkers, reserve, JIT arrival, compliance notes) and a bunker enquiry per refuelling port, from the plan's own numbers. Copy or open in email. Tideline drafts; a person sends. It never books fuel or berths.
- **Audit log.** Every action is appended to `runtime/audit.jsonl`. Each entry carries the SHA-256 of the previous one, so any edit or deletion breaks the chain, and the dashboard shows whether it is intact.

## Just-in-time arrival (new)

If the destination berth is only free later — set *Berth ready at destination* in Advanced, or report a berth queue at the destination — Tideline keeps the best route and refuelling ports and re-optimizes speeds in 1-knot steps so the ship arrives as the berth frees up instead of burning fuel at anchorage. The Voyage tab compares *sail as usual, then wait* with *just in time*: arrival, speeds, hours at anchorage, fuel, CO₂ and cash. Sailing as usual stays an option, so JIT never costs more.

## How sure is the plan? (new)

Every plan comes with ranges from 300 simulated voyages on the recommended route and speeds: arrival (80% range), fuel burned, fuel left in the worst 1 in 10, chance of breaching the reserve, chance of a weather hold, and chance of missing a deadline. Wave and wind error grows with forecast lead time (12% at day 0, +3%/day, max 35%; 40% on seasonal legs); fuel-model error comes from the ship's model residuals; waiting times vary ±50%; heavy seas cost speed. Spreads are documented assumptions, and the numbers are reproducible for the same inputs.

## Fleet assignment (new)

In Fleet mode, *Assign cargo jobs to ships*: enter up to 4 jobs (from, to, cargo, optional deadline) and where each ship is now. Tideline plans every compatible ship–job pair (positioning voyage from where the ship is, then the loaded voyage; tankers carry crude, container ships carry retail and food), checks every possible assignment exactly, and compares the result with a rule of thumb (each job takes the first free compatible ship).

## Insurance coverage (new)

Each ship has a (sample) policy in `data/insurance.json`: insurer, whether it has war-risk cover, notice period, and trading warranties. For every risk zone on a route Tideline decides:
- **covered** — no extra cost;
- **additional premium** — a JWC listed area with war cover: notify underwriters; the war-risk premium is in the plan's cost;
- **held covered** — outside a trading warranty but the insurer covers it for an extra premium (added to the insurance cost);
- **not covered** — no war cover for a listed area, or underwriters cancelled cover there.

By default Tideline **will not route a ship through a zone its policy doesn't cover** (e.g. MV Tern has no war cover, so it goes round the Cape). *Allow routes outside insurance cover* (Advanced) lifts that and flags the uninsured legs instead. The what-if *Insurers cancel Red Sea war cover* (or saying it in chat, or reporting it to an approved voyage) re-routes ships accordingly. Results appear in the Compliance tab, the costs, the map's hazard notes, the infeasibility explanation and the voyage orders. Policies are illustrative, not real terms.

## Sanctions screening (new)

Vessels are screened against the **US Treasury OFAC SDN list** (official, free; downloaded daily into `runtime/sdn.csv`). IMO or MMSI matches block approval until resolved; a name-only hit is flagged as a possible match. Live AIS ships that match are circled in red on the map with the listing. Scope: US OFAC SDN vessels only, not EU/UK/UN lists; screening support, not a legal determination. Set `TIDELINE_SANCTIONS=0` to disable.

## Fuel model calibration on noon reports (new)

Advanced → *Fuel model for this ship*: upload the ship's noon reports as CSV (`speed_kn, distance_nm, fuel_t`, optional `displacement_t, hs_m, headwind_ms, current_kn`). Tideline retrains that ship's regression, keeps prior values for terms the reports barely vary, and reports accuracy on the last 20% of reports before and after. Calibrated models persist in `runtime/calibration/` and are reloaded at startup. *Try sample reports* uses a clearly labeled **synthetic** file (a hull burning about 12% above the textbook formula) to show the workflow.

## Real sea lanes and more ports (new)

Every edge of the route network now follows the **Eurostat SeaRoute MARNET** maritime network (packaged in [searoute-py](https://github.com/genthalili/searoute-py), Apache-2.0) instead of hand-drawn lines: open-ocean waypoints were moved onto the lane network, each edge is the shortest MARNET path between its ends (straits and canals an edge is not meant to use are blocked), simplified to about 3 nm. Singapore → Rotterdam via Suez is now 8,494 nm (about 8,440 nm published). `tools/build_sealanes.py` rebuilds the geometry; it is a development tool, so the app itself stays standard-library only.

Seven more ports: **Mumbai (Nhava Sheva), Busan, Kobe, Hamburg, Piraeus, Mombasa, Lagos (Lekki)** — 22 ports across East Asia, South Asia, the Middle East, Africa and Europe. Their port data are sample values and their today's fuel prices are estimates (labeled). The Americas (Panama, transpacific, transatlantic) are not on the network yet.

## Official navigational warnings (new)

In Today conditions Tideline reads the active **NGA Maritime Safety Information** broadcast warnings (HYDROLANT, HYDROPAC, NAVAREA IV and XII; official, free, refreshed every 3 hours) and keeps the security warnings (attacks, hostile activity, military operations, piracy, mines, GNSS interference) that mention a risk zone on the network — Red Sea / Gulf of Aden, Hormuz / Gulf of Oman, Suez, Malacca, Gulf of Guinea. Each one counts as a **verified** signal in that zone's security score (same capped channel as news) and is listed in Data sources with its warning number and date; the map's hazard notes show them too. Unlike press headlines, these are official. Set `TIDELINE_ADVISORIES=0` to disable.

## AI copilot: operations tools (new)

Besides planning, GPT can now call `voyages_status`, `report_event_to_voyages` (adds a reported event to approved voyages and re-checks them; logged in the audit trail as done by the AI on the operator's request), `screen_vessel` (OFAC) and `assign_jobs`. It cannot approve voyages, accept re-plans, send messages or book anything — a person does that in the Operations panel. Without GPT, "status of my voyages" works with the built-in parser.

## Fleet planning

Switch **Voyage parameters** to **Fleet**, tick 2–6 ships (each with its own fuel on board) and press **Plan fleet** (`POST /api/fleet-plan`). Each ship is optimized, then refueling is coordinated: when fleet ships would bunker at the same port within 24 h, later arrivals are re-planned with a queue (+18 h per ship ahead, a sample assumption) so the optimizer can move their stop or change speed. The Fleet plan panel shows fleet cash cost, fuel, CO₂, EU ETS and last arrival, a row per ship (click for its full plan), the coordination decisions, and chokepoint concentration ("3 of 3 ships transit Bab el-Mandeb"). The map draws each ship in its own color.

## Compliance and regulatory screening

Every plan carries findings in **Compliance & regulatory screening** (decision support, not legal advice):
- **EU ETS (cost):** voyages to or from an EU port (Rotterdam here) surrender allowances for 50% of voyage CO₂ (100% if both ends are EU; full phase-in from 2026). Bunkering-only stops are not ports of call. EUA ≈ €82/t (Jul 2026, flagged stale) at EUR/USD 1.121 (16 Oct 2026). The cost is included in cash, so it affects routing.
- **JWC Listed Areas (action):** transits of the Gulf of Aden/southern Red Sea, Persian Gulf/Gulf of Oman or Gulf of Guinea, and calls in listed countries (UAE), per JWLA-035 (16 Sep 2026).
- **Vessel/cargo links:** *Saudi-linked* excludes Bab el-Mandeb (Houthi embargo declared Jul 2026); *Iran-linked* excludes Hormuz (US blockade of Iranian ports).
- **Oil cargo:** MARPOL Annex I reminder. **Tariffs:** route-independent unless there is a transshipment; bunkering stops are not customs entries (no tariff schedules are modelled).

## Live ships (AIS)

Get a free key at aisstream.io and start the server with it: `AISSTREAM_API_KEY=... python3 server.py`. Click **⛴ Live ships** on the map: real ships near Bab el-Mandeb, Suez, Hormuz, Singapore, Rotterdam and the Cape appear as icons pointing along their course, with dotted trails, and move between reports along their reported speed and course. Click a ship to see its details, **Track worldwide** (follows chosen MMSIs anywhere; AISStream allows one connection per key, so tracking replaces the chokepoint watch until you press *Watch chokepoints*), or **Plan from here**: the optimizer snaps the live position onto the network and plans to your destination from there, even if the ship is inside a zone that just closed.

No key, or no internet? For rehearsal, `python3 tests/fake_aisstream.py 8765` streams three clearly fake test vessels; start the server with `AISSTREAM_WS_URL=ws://127.0.0.1:8765 AISSTREAM_API_KEY=test`. Never present the fake stream as live data.

## Cargo, hazards and custom weights

- **Cargo segment** (crude oil and tankers, retail containers, food and reefer, dry bulk; defaults from the ship): the news watch adds segment-specific searches, tags headlines about your cargo, and counts those twice toward a zone's (capped) news risk.
- **Hazard map:** risk zones are colored by the level the planner computed (closed, high, elevated, normal), with ▲ labels; hover for the reasons (security, war-risk premium, traffic drop, headlines). Ports get ▲ warning markers when closed, too shallow for the selected ship, inside a high-risk zone, reachable only through a risky strait, or congested.
- **Weights:** the Optimization objective presets set three sliders: **risk aversion** (shown as dollars per risk point), **carbon price** ($/t CO₂) and **value of time** (multiple of the daily operating cost). Move any slider to use custom weights (`weights` in the API); the cost breakdown shows each weight's effect separately from real cash.

## Mid-voyage re-planning

The **Voyage progress** slider puts the ship at sea on the plan it committed to at departure, with the fuel it would have left. Any change then re-plans **from the ship's position**: the optimizer adds the ship as a temporary node with routes forward and back (no turning inside a canal), keeps the original departure clock for deadlines and weather, and does not re-charge war-risk premiums already paid. It also solves "stay on the committed route" and recommends **Stay on course** or **Change course now**. Changing route is recommended only when it beats staying on course by more than $25,000 on the decision score, or when the committed route is blocked. The chat keeps the ship's position, so "what if we go fastest?" is answered from where the ship is.

## Today's conditions vs normal times

A switch in **World conditions** chooses what the optimizer believes about the world:

- **Today (default):** a sourced snapshot (`data/today.json`, as of 16 Oct 2026) plus live feeds. VLSFO fuel prices (Singapore, Rotterdam, Fujairah published by Bunker Index; Shanghai and Durban derived from ENGINE market reports; other ports estimated from the nearest benchmark), war-risk premiums for Bab el-Mandeb and Hormuz (Al Jazeera, Jul 2026, flagged as stale), the 12% Suez surcharge, security assessments from Lloyd's List Intelligence and Red Team Analysis, and **live IMF PortWatch chokepoint traffic** compared with the same weeks of 2023. Under today's conditions the Cape usually ranks first for Asia–Europe, as most carriers currently route.
- **Normal times:** the pre-crisis baseline with sample prices and normal-times risk, for the classic "close the Red Sea" demo.

The **Data sources** panel lists every input behind the current plan with its basis (live, published, derived, estimated, assessment, sample, simulated), date and link. Starting fuel is valued at market price, so cash totals reflect fuel actually burned.

`GET /api/conditions` returns the snapshot and the PortWatch status. PortWatch refreshes every 6 hours; set `TIDELINE_PORTWATCH=0` to skip it. Citation: Sources: UN Global Platform; IMF PortWatch (portwatch.imf.org).

## Live weather in the plan

The server keeps a live **Open-Meteo** forecast of **wave height, wind and ocean currents** at about 150 points along the route network, refreshed every hour in the background (`live_weather.py`). The optimizer uses it for every segment the ship reaches within the 7-day forecast window: waves and head wind add fuel and risk, currents speed the ship up or slow it down, and waves above a ship's safe limit rule that segment out. Later segments use sample seasonal values.

The Voyage plan shows how many segments used the live forecast, and the checkpoint table shows each segment's sea state and its source (live, live → seasonal, seasonal, or storm event). When a new forecast arrives, the dashboard re-plans automatically and says so in the chat. Turn off **Use live weather forecast** for a fully repeatable offline demo, or start with `TIDELINE_LIVE_WEATHER=0` to skip fetching. `GET /api/weather-status` shows the current snapshot.

## AI chat: GPT or offline

The **Ask Tideline** panel turns plain English (or a pasted news report) into planner inputs, runs the optimizer, and explains the result. It works in two modes:

- **GPT mode:** start the server with an OpenAI key. The model calls the `plan_voyage` tool; the backend runs the real optimizer; the model explains the tool result. Default model `gpt-5-mini` (override with `OPENAI_MODEL`).
- **Offline mode (no key):** a built-in rule parser handles closures, threats, storms, port congestion and fuel prices, ship and route changes, objectives, deadlines and "what if we arrive a day later?", with a templated explanation. GPT mode also falls back to this automatically if the API call fails, so the demo never depends on Wi-Fi.

Either way, every figure in the reply is checked against the planner output. The chat shows "✓ Every figure traced to the planner", or lists any figure it cannot trace. Pasted text is treated as data, never as instructions, and extracted events keep their source.

```bash
# macOS / Linux
OPENAI_API_KEY=sk-your-key python3 server.py

# Windows PowerShell
$env:OPENAI_API_KEY="sk-your-key"; py -3 server.py
```

The key stays in the server process; it is never sent to the browser. Don't commit it to the repository or paste it into the chat.

## Project structure

```
pelagic-maritime-mvp/
├── index.html          # Dashboard, CSS, SVG map layers
├── app.js              # Route optimizer, UI state, dynamic SVG, economics
├── server.py           # Zero-dependency local web server, API proxies and planning API
├── optimizer.py        # Route x speed x refueling search, constraints, explanations
├── fuel_model.py       # Physics formula + regression trained on simulated data
├── risk_model.py       # Transparent weather / security / traffic risk scoring
├── conditions.py       # Live forecast, sample seasonal sea state, storm events
├── live_weather.py     # Open-Meteo waves, wind, currents along the network
├── portwatch.py        # IMF PortWatch chokepoint transits vs 2023
├── fleet.py            # Fleet planning and refueling coordination
├── compliance.py       # EU ETS, JWC listed areas, sanctions-driven exclusions, tariff note
├── ais.py              # Live AIS (AISStream.io) with a stdlib websocket client
├── data/regulatory.json # Sourced compliance rules
├── data/today.json     # Sourced snapshot of today's conditions
├── ai_layer.py         # GPT tool calling, offline parser, number tracing
├── ops.py              # Approval, monitoring, re-planning at sea, dispatch drafts, audit hash chain
├── uncertainty.py      # Monte Carlo ranges for arrival and fuel
├── assign.py           # Which ship takes which cargo job
├── calibration.py      # Retrain a ship's fuel model on noon reports
├── sanctions.py        # OFAC SDN vessel screening
├── insurance.py        # Coverage check against each ship's (sample) policy; data/insurance.json
├── advisories.py       # Official NGA navigational warnings (security) on risk zones
├── tools/build_sealanes.py  # Dev tool: rebuild lane geometry from the MARNET network
├── runtime/            # Created on first run: voyages, audit log, sanctions list, calibrations (not committed)
├── data/network.json   # Sample waypoint network, ports, risk zones
├── data/fleet.json     # Five sample vessel profiles
├── routes.json         # Three trade lanes / two alternatives per lane (current dashboard)
├── assets/land.svg     # Embedded coastlines (Natural Earth low-res)
├── tests/test_backend.py
├── tests/test_ai.py
├── tests/test_live_weather.py
├── tests/test_portwatch.py
├── tests/test_fleet_compliance_ais.py
├── tests/test_ops.py, test_jit.py, test_uncertainty.py, test_assign_calibration.py
├── tests/fake_aisstream.py  # local fake AIS stream for tests and rehearsal
├── demo-screenshot.png
├── PITCH.md
├── run-windows.bat
└── README.md
```

## Tests

```bash
python3 -m unittest discover -s tests -v
node --check app.js  # optional, only if Node.js already installed
```

## Upgrading this to a startup-quality system

1. **Route graph:** Built: lanes follow the MARNET network. Next: route between any two of thousands of ports directly on MARNET (not via the 22-port planning graph), the Americas and Panama, and licensed lane data with traffic separation schemes.
2. **Real vessel state:** Authorized AIS feed (MMSI, position, course/speed), port turnaround, berth queues, and actual ETA forecasts from historical voyages.
3. **Security confidence:** Built: NGA official warnings as verified signals, separate from press. Next: UKMTO/JMIC advisories, geo-boundaries from warning text, and operator acknowledgment before a warning becomes a closure.
4. **Real commercial cost:** Official Suez/Panama rate cards by vessel category, fuel grade/bunker price curves, port fees, emissions/carbon charges, war-risk premiums, insurance constraints.
5. **Time-dependent optimization:** Optimize over a validated sea-lane graph using ensemble weather predictions, currents, sea state, speed decisions, emissions, and uncertainty intervals. Incorporate a proper multiobjective Pareto frontier.
6. **Human-centered operations:** Built: approval, alerts, audit log, voyage-order and bunker-enquiry drafts. Next: sending through the operator's email/chat systems and two-person approval for diversions.

What still needs data access or contracts (not buildable with free data): live berth availability and terminal queues, live bunker prices for every port, commercial quotes and charter terms, full tariff schedules, and actually booking fuel or berths. The app labels these honestly instead of pretending.

## Architecture and safety

- No API secrets; no `.env` setup; no external JavaScript runtime dependencies.
- The Python server defaults to `127.0.0.1` rather than `0.0.0.0`.
- News headlines are escaped in the UI; server requests are fixed to known trusted feed endpoints.
- Neither an alleged geopolitical event nor any simulated disruption is represented as an independently confirmed maritime restriction.
- **Not for navigation, bridge operations, underwriting, or insurance decisions.**
