// Route trade-off helpers: how the decision score splits into cost, carbon, risk and time, which routes are Pareto-efficient,
// and a one-line reason why the winner won. Pure functions on data the plan already returns; app.js draws them.
(function (root) {
  // The decision score is cash + carbon shadow price x CO2 + risk loss x route risk + time premium (see optimizer.py).
  const PARTS = [['cost', 'cash_total_usd'], ['carbon', 'carbon_shadow_usd'], ['risk', 'risk_adjustment_usd'], ['time', 'time_preference_usd']];

  // Share of the score each part makes up, whole percentages that add up to exactly 100 (largest remainder).
  function scoreShares(decision) {
    const values = PARTS.map(([, key]) => Math.max(0, Number(decision && decision[key]) || 0));
    const total = values.reduce((a, b) => a + b, 0);
    const out = {cost: 0, carbon: 0, risk: 0, time: 0};
    if (!(total > 0)) return out;
    const exact = values.map(v => 100 * v / total), floors = exact.map(Math.floor);
    let left = 100 - floors.reduce((a, b) => a + b, 0);
    const order = exact.map((e, i) => [e - floors[i], i]).sort((a, b) => b[0] - a[0] || a[1] - b[1]);
    for (const [, i] of order) { if (left-- > 0) floors[i] += 1; }
    PARTS.forEach(([name], i) => { out[name] = floors[i]; });
    return out;
  }

  // Ids of the routes that no other route beats on cost, CO2 and risk at once (lower is better on all three).
  function paretoFront(points) {
    const efficient = new Set();
    for (const p of points) {
      const beaten = points.some(q => q !== p && q.cost <= p.cost && q.co2 <= p.co2 && q.risk <= p.risk && (q.cost < p.cost || q.co2 < p.co2 || q.risk < p.risk));
      if (!beaten) efficient.add(p.id);
    }
    return efficient;
  }

  const money = n => n >= 1e6 ? `$${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `$${Math.round(n / 1e3)}k` : `$${Math.round(n)}`;
  const tonnes = n => `${Math.round(n).toLocaleString('en-US')} t`;
  const dayText = n => { const d = Math.abs(n) >= 1 ? Math.round(n) : Number(n.toFixed(1)); return `${d} day${d === 1 ? '' : 's'}`; };
  // 'better' means lower on every measure here; a difference under half a percent (one point for risk) counts as level.
  const cmp = (a, b, tol) => (Math.abs(a - b) <= tol ? 0 : a < b ? -1 : 1);

  // One line on why `best` was chosen over `other`: what it wins on, and what that costs.
  function whyLine(best, other) {
    if (!best || !other) return '';
    const measures = [
      {name: 'cost', c: cmp(best.cost, other.cost, 0.005 * Math.max(best.cost, other.cost)), nums: `${money(best.cost)} vs ${money(other.cost)}`,
        worse: `costs ${money(best.cost - other.cost)} more`},
      {name: 'time', c: cmp(best.days, other.days, 0.005 * Math.max(best.days, other.days)), nums: `${Math.round(best.days)} vs ${dayText(other.days)}`,
        worse: `takes ${dayText(best.days - other.days)} longer`},
      {name: 'CO₂', c: cmp(best.co2, other.co2, 0.005 * Math.max(best.co2, other.co2)), nums: `${tonnes(best.co2)} vs ${tonnes(other.co2)}`,
        worse: `emits ${tonnes(best.co2 - other.co2)} more CO₂`},
      {name: 'risk', c: cmp(best.risk, other.risk, 1), nums: `${best.risk} vs ${other.risk}`, worse: null},
    ];
    const better = measures.filter(m => m.c < 0), worse = measures.filter(m => m.c > 0);
    if (!better.length && !worse.length) return `${best.label} and ${other.label} are level on cost, time, CO₂ and risk.`;
    const list = names => names.length < 2 ? names.join('') : `${names.slice(0, -1).join(', ')} and ${names.at(-1)}`;
    const head = better.length ? `${best.label} wins on ${better.length === 1 ? `${better[0].name} (${better[0].nums})` : list(better.map(m => m.name))}`
      : `${best.label} has the best overall score`;
    const comparatives = worse.filter(m => m.worse), riskWorse = worse.find(m => m.name === 'risk');
    const clauses = [];
    if (comparatives.length) clauses.push(`it ${list(comparatives.map(m => m.worse))} than ${other.label}`);
    if (riskWorse) clauses.push(`its risk is higher (${riskWorse.nums})`);
    const against = comparatives.length ? '' : ` against ${other.label}`;
    return head + against + (clauses.length ? '; ' + clauses.join('; ') : '') + '.';
  }

  const api = {scoreShares, paretoFront, whyLine};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.TLCompare = api;
})(typeof window !== 'undefined' ? window : globalThis);
