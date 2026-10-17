# Tideline — 90-second hackathon pitch

**Opening (0–15s):** "Every day, operators move billions of dollars of cargo through a handful of chokepoints. A voyage that looks cheapest on Monday can become the wrong decision when weather, security, or canal costs change. Yet shipping teams often have to reconcile separate forecasts, intelligence feeds, and spreadsheets."

**Product (15–35s):** "Tideline is a decision layer for global shipping. Choose a trade lane, a ship speed, and an operating objective. We compare Suez and Cape routes, project an indicative ETA and cost, and explain the trade-off. Open-Meteo forecast data affects upcoming passage conditions, while current press coverage contributes strictly labeled risk indicators."

**Live demo (35–65s):** "Here Singapore to Rotterdam prefers Suez. Suppose a disruption blocks Bab el-Mandeb. In one click, Tideline removes the invalid corridor, visualizes the Cape diversion, and updates the modeled budget and arrival date. Now select a ship departing Jebel Ali and close Hormuz: both routes are blocked. That honest answer is crucial; a maritime operating system cannot invent a physically impossible detour."

**Business (65–80s):** "We would sell this as a dispatch and decision-support system to small and mid-sized ship operators, charterers, and freight forwarders, starting with better route comparison and disruption response — and expanding into voyage operations, compliance, cost accounting, and ETA prediction."

**Close (80–90s):** "We aren't building another vessel map. We're building the system that recommends the next best operational decision, with evidence and human approval."

## Judge Q&A

**Is it AI?** The current MVP is a transparent, deterministic decision engine, not a trained AI model or a fully agentic operating system. A later iteration can use AI for grounding and summarizing incident reports with source traceability and approvals. Don't claim AI features we haven't built.

**Why is ETA credible?** It is a demonstrable route-level planning estimate, not an operational maritime ETA. Distances are from curated waypoints; the vessel/economic inputs are transparent assumptions. Multiweek weather uncertainty is explicitly not inferred beyond the forecast window. A production model would require AIS history, graph routing, and calibrated vessel performance.

**Can it actually sail around Hormuz?** Not if the origin is inside the Persian Gulf and Hormuz is closed. The demo exposes this feasibility constraint on purpose.

**Can I trust an attack alert?** No — not from a news keyword classifier. We separate potential press signals from verified navigational advisories. Human review is required before route decisions.

**What does YC's RFS have to do with it?** The Fall 2026 request for new operating systems for the physical world describes replacing dated dispatch/fleet/asset workflows with software able to coordinate decisions. Shipping fleet intelligence fits that frame.
