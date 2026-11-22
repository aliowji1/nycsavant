/* Your commute: trip time and wait time between two stations, across the day. */
S.pages = S.pages || {};

S.pages.commute = async (root, params) => {
  const [meta, net] = await Promise.all([S.load("meta.json"), S.load("network.json")]);
  const name = new Map(net.stations.map((s) => [s[0], s[1]]));
  const routes = S.sortRoutes(Object.keys(net.lines));
  let route = params.get("route") || "Q";
  if (!routes.includes(route)) route = routes[0];
  const weekdays = meta.dates.filter((d) => { const w = new Date(d + "T12:00").getDay(); return w > 0 && w < 6; });
  root.innerHTML = `
    <div class="kicker">Your commute</div>
    <h1>How long will it actually take?</h1>
    <p class="lede">Pick a line and two stations. Each dot in the top chart is one train's trip between them, and each dot below is how long
    the gap was since the previous train. The shaded band covers the middle 80% of trips and the dark line is the median.</p>
    <div class="controls"><div class="routes-picker" id="c-rp"></div></div>
    <div class="controls">
      <label>From<select id="c-from"></select></label>
      <label>To<select id="c-to"></select></label>
      <label>Days<select id="c-days">
        <option value="wk5">Last 5 weekdays</option><option value="wk">All weekdays (${weekdays.length})</option>
        ${meta.dates.map((d) => `<option value="${d}">${S.fmt.date(d)}</option>`).join("")}
      </select></label>
    </div>
    <div class="tiles" id="c-tiles"></div>
    <div class="card"><h3 style="margin-top:0">Time on the train</h3><div class="chart" id="c-dur"></div>
      <div class="legend"><span><i style="background:var(--series-1);opacity:.35"></i>Middle 80%</span><span><i style="background:var(--series-1)"></i>Median</span><span>Each dot is one trip</span></div></div>
    <div class="card" style="margin-top:14px"><h3 style="margin-top:0">Time waiting on the platform (gap between trains)</h3><div class="chart" id="c-wait"></div>
      <div class="legend"><span><i style="background:var(--series-2);opacity:.35"></i>Middle 80%</span><span><i style="background:var(--series-2)"></i>Median</span><span>Your average wait is about half the gap</span></div></div>`;
  const rp = root.querySelector("#c-rp");
  rp.innerHTML = routes.map((r) => `<button data-r="${r}" class="${r === route ? "on" : ""}">${S.bullet(r)}</button>`).join("");
  const fromSel = root.querySelector("#c-from"), toSel = root.querySelector("#c-to"), daysSel = root.querySelector("#c-days");
  function fillStations(keep) {
    const st = net.lines[route].dirs["1"].stations;
    const opts = st.map((s, i) => `<option value="${i}">${name.get(s) || s}</option>`).join("");
    fromSel.innerHTML = opts; toSel.innerHTML = opts;
    fromSel.value = keep && params.get("from") != null ? params.get("from") : Math.floor(st.length * 0.25);
    toSel.value = keep && params.get("to") != null ? params.get("to") : Math.floor(st.length * 0.7);
  }
  fillStations(true);
  rp.querySelectorAll("button").forEach((b) => (b.onclick = () => {
    route = b.dataset.r; rp.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b)); fillStations(false); draw();
  }));
  [fromSel, toSel, daysSel].forEach((s) => (s.onchange = draw));

  async function draw() {
    const a = +fromSel.value, b = +toSel.value;
    history.replaceState(null, "", `#/commute?route=${route}&from=${a}&to=${b}`);
    const v = daysSel.value;
    const days = v === "wk5" ? weekdays.slice(-5) : v === "wk" ? weekdays : [v];
    root.querySelector("#c-dur").innerHTML = root.querySelector("#c-wait").innerHTML = `<div class="loading">Loading ${days.length} day${days.length > 1 ? "s" : ""}…</div>`;
    const files = await Promise.all(days.map((d) => S.load(`marey/${d}.json`)));
    const dir = b > a ? 1 : 0;
    const trips = [], waits = [];
    files.forEach((f) => {
      const deps = [];
      for (const t of f[route] || []) {
        if (t[0] !== dir) continue;
        const ia = t[1].indexOf(a), ib = t[1].indexOf(b);
        if (ia < 0 || ib < 0 || ib <= ia) continue;
        const ta = t[2][ia];
        deps.push(ta);
        trips.push({ t: ta, v: (t[2][ib] - ta) / 60, id: t[3] });
      }
      deps.sort((x, y) => x - y);
      for (let i = 1; i < deps.length; i++) waits.push({ t: deps[i], v: (deps[i] - deps[i - 1]) / 60 });
    });
    const q = (arr, p) => d3.quantile(arr.map((d) => d.v).sort(d3.ascending), p);
    const peak = (arr) => arr.filter((d) => d.t >= 7 * 3600 && d.t < 10 * 3600);
    root.querySelector("#c-tiles").innerHTML = trips.length ? `
      <div class="tile"><div class="k">Median ride</div><div class="v">${q(trips, 0.5).toFixed(1)} min</div><div class="s">${trips.length} trips</div></div>
      <div class="tile"><div class="k">Slow day (90th pct)</div><div class="v">${q(trips, 0.9).toFixed(1)} min</div><div class="s">plan for this if it matters</div></div>
      <div class="tile"><div class="k">Rush-hour gap</div><div class="v">${peak(waits).length ? q(peak(waits), 0.5).toFixed(1) : "–"} min</div><div class="s">median, 7 to 10a</div></div>
      <div class="tile"><div class="k">Worst gap</div><div class="v">${d3.max(waits, (d) => d.v)?.toFixed(0) ?? "–"} min</div><div class="s">longest wait seen</div></div>` : "";
    if (a === b || !trips.length) {
      root.querySelector("#c-dur").innerHTML = `<div class="empty">No trains ran between these stations${a === b ? " (pick two different stations)" : ""}.</div>`;
      root.querySelector("#c-wait").innerHTML = "";
      return;
    }
    bandChart(root.querySelector("#c-dur"), trips, "var(--series-1)", "min ride");
    bandChart(root.querySelector("#c-wait"), waits, "var(--series-2)", "min gap");
  }
  draw();
};

function bandChart(el, pts, color, unit) {
  el.innerHTML = "";
  const W = Math.max(600, el.clientWidth), H = 240, m = { t: 10, r: 16, b: 28, l: 44 };
  const x = d3.scaleLinear([4 * 3600, 26 * 3600], [m.l, W - m.r]);
  const y = d3.scaleLinear([0, d3.quantile(pts.map((d) => d.v).sort(d3.ascending), 0.995) * 1.1], [H - m.b, m.t]).nice();
  const svg = d3.select(el).append("svg").attr("width", W).attr("height", H);
  svg.append("g").attr("class", "gridline").attr("transform", `translate(${m.l},0)`).call(d3.axisLeft(y).ticks(5).tickSize(-(W - m.l - m.r)).tickFormat(""));
  svg.append("g").attr("class", "axis").attr("transform", `translate(${m.l},0)`).call(d3.axisLeft(y).ticks(5).tickFormat((d) => `${d}m`));
  svg.append("g").attr("class", "axis").attr("transform", `translate(0,${H - m.b})`)
    .call(d3.axisBottom(x).tickValues(d3.range(4, 27, 2).map((h) => h * 3600)).tickFormat(S.fmt.clock));
  // rolling 30-min window quantiles every 10 min
  const sorted = pts.slice().sort((a, b) => a.t - b.t);
  const band = [];
  for (let t = 4 * 3600; t <= 26 * 3600; t += 600) {
    const w = sorted.filter((d) => Math.abs(d.t - t) <= 1800).map((d) => d.v).sort(d3.ascending);
    if (w.length >= 4) band.push({ t, lo: d3.quantile(w, 0.1), mid: d3.quantile(w, 0.5), hi: d3.quantile(w, 0.9) });
  }
  svg.append("path").datum(band).attr("fill", color).attr("fill-opacity", 0.18)
    .attr("d", d3.area().x((d) => x(d.t)).y0((d) => y(d.lo)).y1((d) => y(d.hi)).curve(d3.curveMonotoneX));
  svg.append("g").selectAll("circle").data(pts.filter((d) => d.t >= 4 * 3600 && d.t <= 26 * 3600)).join("circle")
    .attr("cx", (d) => x(d.t)).attr("cy", (d) => y(Math.min(d.v, y.domain()[1]))).attr("r", 1.8).attr("fill", color).attr("fill-opacity", 0.45);
  svg.append("path").datum(band).attr("fill", "none").attr("stroke", color).attr("stroke-width", 2)
    .attr("d", d3.line().x((d) => x(d.t)).y((d) => y(d.mid)).curve(d3.curveMonotoneX));
  const cross = svg.append("line").attr("y1", m.t).attr("y2", H - m.b).attr("stroke", "var(--ink-3)").style("display", "none");
  svg.append("rect").attr("x", m.l).attr("y", m.t).attr("width", W - m.l - m.r).attr("height", H - m.t - m.b).attr("fill", "transparent")
    .on("mousemove", (ev) => {
      const [px] = d3.pointer(ev);
      const t = x.invert(px);
      const b = band.reduce((p, c) => (Math.abs(c.t - t) < Math.abs(p.t - t) ? c : p), band[0]);
      if (!b) return;
      cross.style("display", null).attr("x1", x(b.t)).attr("x2", x(b.t));
      S.tip(`<b>Around ${S.fmt.clock(b.t)}</b><br>Median ${b.mid.toFixed(1)} ${unit}<br>Middle 80%: ${b.lo.toFixed(1)} to ${b.hi.toFixed(1)}`, ev);
    }).on("mouseleave", () => { cross.style("display", "none"); S.untip(); });
}
