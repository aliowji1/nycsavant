/* Savant: leaderboards and stat lines for buses, scheduled subway trips, and lines. */
S.pages = S.pages || {};

S.pages.savant = async (root, params) => {
  const view = params.get("v") || "buses";
  root.innerHTML = `
    <div class="kicker">Savant</div>
    <h1>Every bus and every train trip, ranked.</h1>
    <p class="lede">Each NYC bus gets a stat line from its own trips: how often it reached the end of the line on schedule,
    how much time it saved compared with a typical trip at that hour, and <b>TAR</b>, trips above replacement.
    Subway cars aren't public, so the subway is ranked by scheduled trip (the 8:14a Q from 96 St, every weekday) and by line.</p>
    <div class="controls"><div class="seg" id="sv-tabs">
      <button data-v="buses">Buses</button><button data-v="trips">Subway trips</button><button data-v="lines">Subway lines</button>
    </div></div>
    <div id="sv-body"><div class="loading">Loading…</div></div>`;
  root.querySelectorAll("#sv-tabs button").forEach((b) => {
    b.classList.toggle("on", b.dataset.v === view);
    b.onclick = () => { location.hash = `#/savant?v=${b.dataset.v}`; };
  });
  const body = root.querySelector("#sv-body");
  if (view === "buses") return busesView(body, params);
  if (view === "trips") return tripsView(body, params);
  return linesView(body);
};

/* ------------------------------------------------------------------ percentile bars */
function pctBars(metrics) {
  return `<div class="pct-scale"><div></div><div><span>Poor</span><span>Average</span><span>Great</span></div><div></div></div>` +
    metrics.map((m) => {
      if (m.p == null) return "";
      const c = S.savantColor(m.p);
      return `<div class="pct"><div class="name">${m.name}</div>
        <div class="track"><div class="fill" style="width:${m.p}%;background:${c}"></div>
        <div class="dot" style="left:${m.p}%;background:${c};color:${m.p > 25 && m.p < 75 ? "var(--ink)" : "#fff"}">${m.p}</div></div>
        <div class="val">${m.v}</div></div>`;
    }).join("");
}

/* ------------------------------------------------------------------ buses */
async function busesView(body, params) {
  const blob = await S.load("savant_buses.json");
  const rows = S.objs(blob);
  rows.forEach((r) => (r.rank_line_txt = r.rank_line ? `${r.rank_line} of ${r.line_size}` : "–"));
  const q = rows.filter((r) => r.qualified);
  const totTrips = d3.sum(rows, (r) => r.trips), totOn = d3.sum(rows, (r) => r.on_sched);
  const best = q[0];
  const routes = Array.from(new Set(rows.map((r) => r.line))).sort();
  body.innerHTML = `
    <div class="tiles">
      <div class="tile"><div class="k">Buses tracked</div><div class="v">${S.fmt.num(rows.length)}</div><div class="s">${q.length} with ${S.cfg.min_trips}+ trips</div></div>
      <div class="tile"><div class="k">Trips</div><div class="v">${S.fmt.num(totTrips)}</div><div class="s">complete, start to end</div></div>
      <div class="tile"><div class="k">On schedule</div><div class="v">${S.fmt.pct(totOn / totTrips)}</div><div class="s">1 min early to 5 min late</div></div>
      <div class="tile"><div class="k">Top bus</div><div class="v">#${best ? best.fleet : "–"}</div><div class="s">${best ? `${S.fmt.num(best.tar, 1)} TAR on the ${best.line}` : ""}</div></div>
    </div>
    <div id="bus-detail"></div>
    <h2>Leaderboard</h2>
    <div class="controls">
      <label>Find a bus<input type="search" id="bus-q" placeholder="Fleet number, e.g. ${best ? best.fleet : "5701"}"></label>
      <label>Route<select id="bus-r"><option value="">All routes</option>${routes.map((r) => `<option>${r}</option>`).join("")}</select></label>
      <label>Show<select id="bus-qual"><option value="1">Qualified (${S.cfg.min_trips}+ trips)</option><option value="0">All buses</option></select></label>
    </div>
    <div class="tablewrap" id="bus-table"></div>
    <p class="note">Fleet numbers are the numbers painted on the bus. A bus is ranked on the route it ran most.
    Replacement level for each route is the on-schedule rate of its 20th-percentile bus.</p>`;
  const cols = [
    { key: "fleet", label: "Bus", cls: "l", html: (r) => `<b>#${r.fleet}</b>` },
    { key: "line", label: "Route", cls: "l", html: (r) => S.busRoute(r.line) },
    { key: "trips", label: "Trips", fmt: S.fmt.num },
    { key: "on_sched_rate", label: "On sched", fmt: S.fmt.pct },
    { key: "time_saved", label: "Saved/trip", fmt: S.fmt.mmss, title: "Typical runtime minus actual, minutes:seconds" },
    { key: "median_delay", label: "Med. delay", fmt: S.fmt.mmss, desc: false },
    { key: "tar", label: "TAR", fmt: (x) => S.fmt.num(x, 1) },
    { key: "rank_line", label: "Route rank", html: (r) => r.rank_line_txt, desc: false },
  ];
  const tbl = S.table(body.querySelector("#bus-table"), cols, q, { sort: "tar", onClick: (r) => show(r.fleet) });
  const filter = () => {
    const s = body.querySelector("#bus-q").value.trim(), rt = body.querySelector("#bus-r").value;
    const qual = body.querySelector("#bus-qual").value === "1";
    tbl.redraw(rows.filter((r) => (!qual || r.qualified) && (!rt || r.line === rt) && (!s || r.fleet.includes(s))));
  };
  body.querySelectorAll("#bus-q,#bus-r,#bus-qual").forEach((e) => (e.oninput = filter));

  function show(fleet) {
    const r = rows.find((x) => x.fleet === fleet);
    if (!r) return;
    history.replaceState(null, "", `#/savant?v=buses&bus=${fleet}`);
    const peers = q.filter((x) => x.line === r.line);
    const P = (k, inv) => S.pctile(peers.map((x) => x[k]), r[k], inv);
    const daily = blob.daily[fleet] || [];
    const el = body.querySelector("#bus-detail");
    el.innerHTML = `<div class="card">
      <div class="kicker">Bus #${r.fleet}</div>
      <h2 style="margin:4px 0 2px">${S.busRoute(r.line)} &nbsp;Ranked ${r.rank_line_txt} on the ${r.line}</h2>
      <div class="muted">Also ran: ${r.lines.split(" ").filter((x) => x !== r.line).join(", ") || "no other routes"} · seen on ${r.days} days</div>
      <div class="statline">TRIPS ${r.trips} · ON-SCHED ${r.on_sched}/${r.trips} (${S.fmt.pct(r.on_sched_rate)}) · SAVED/TRIP ${S.fmt.mmss(r.time_saved)} · MED DELAY ${S.fmt.mmss(r.median_delay)} · TAR ${S.fmt.num(r.tar, 1)} · REPL ${S.fmt.pct(r.replacement_rate)}</div>
      <div class="grid2">
        <div><h3>Percentile vs. other ${r.line} buses</h3>${r.qualified ? pctBars([
          { name: "Trips above replacement", p: P("tar"), v: S.fmt.num(r.tar, 1) },
          { name: "On-schedule rate", p: P("on_sched_rate"), v: S.fmt.pct(r.on_sched_rate) },
          { name: "Time saved per trip", p: P("time_saved"), v: S.fmt.mmss(r.time_saved) },
          { name: "Median terminal delay", p: P("median_delay", true), v: S.fmt.mmss(r.median_delay) },
          { name: "Workload (trips)", p: P("trips"), v: S.fmt.num(r.trips) },
        ]) : `<p class="note">Not enough trips to rank yet (${r.trips} of ${S.cfg.min_trips}).</p>`}</div>
        <div><h3>Trips by day</h3><div class="chart" id="bus-daily"></div>
          <div class="legend"><span><i style="background:var(--series-1)"></i>On schedule</span><span><i style="background:var(--rule)"></i>Off schedule</span></div></div>
      </div></div>`;
    dailyBars(el.querySelector("#bus-daily"), daily);
    el.scrollIntoView({ behavior: "smooth", block: "start" });
  }
  if (params.get("bus")) show(params.get("bus"));
}

function dailyBars(el, daily) {
  const W = Math.max(320, el.clientWidth || 480), H = 200, m = { t: 8, r: 8, b: 28, l: 30 };
  const svg = d3.select(el).append("svg").attr("width", W).attr("height", H);
  const x = d3.scaleBand(daily.map((d) => d[0]), [m.l, W - m.r]).padding(0.25);
  const y = d3.scaleLinear([0, d3.max(daily, (d) => d[1]) || 1], [H - m.b, m.t]).nice();
  svg.append("g").attr("class", "gridline").attr("transform", `translate(${m.l},0)`)
    .call(d3.axisLeft(y).ticks(4).tickSize(-(W - m.l - m.r)).tickFormat(""));
  svg.append("g").attr("class", "axis").attr("transform", `translate(${m.l},0)`).call(d3.axisLeft(y).ticks(4));
  svg.append("g").attr("class", "axis").attr("transform", `translate(0,${H - m.b})`)
    .call(d3.axisBottom(x).tickFormat((d) => d.slice(5)).tickValues(x.domain().filter((_, i, a) => a.length < 10 || i % 2 === 0)));
  const g = svg.append("g");
  daily.forEach(([date, n, on]) => {
    const bw = x.bandwidth(), x0 = x(date);
    // two segments, 2px surface gap between them, rounded top on the data end
    g.append("rect").attr("x", x0).attr("width", bw).attr("y", y(on)).attr("height", y(0) - y(on)).attr("fill", "var(--series-1)");
    if (n > on) g.append("path").attr("fill", "var(--rule)")
      .attr("d", roundTop(x0, y(n), bw, Math.max(0, y(on) - y(n) - 2), 3));
    g.append("rect").attr("x", x0 - 2).attr("width", bw + 4).attr("y", m.t).attr("height", H - m.b - m.t).attr("fill", "transparent")
      .on("mousemove", (ev) => S.tip(`<b>${S.fmt.date(date)}</b><br>${n} trips · ${on} on schedule`, ev)).on("mouseleave", S.untip);
  });
}
function roundTop(x, y, w, h, r) {
  r = Math.min(r, w / 2, h);
  return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
}

/* ------------------------------------------------------------------ subway trips */
async function tripsView(body, params) {
  const top = await S.load("slots/top.json");
  const lines = S.objs(await S.load("lines.json"));
  const routes = S.sortRoutes(lines.map((l) => l.route_id));
  let route = params.get("route") || "Q";
  if (!routes.includes(route)) route = routes[0];
  body.innerHTML = `
    <p class="note">A scheduled trip is one row of the timetable, like the 8:14a weekday Q from 96 St. It runs once per service day,
    so its stat line builds up over weeks. TAR compares it with a replacement-level trip on the same line.</p>
    <div class="grid2">
      <div><h2>Most reliable scheduled trips</h2><div class="tablewrap" id="best"></div></div>
      <div><h2>Least reliable</h2><div class="tablewrap" id="worst"></div></div>
    </div>
    <h2>Every scheduled trip on a line</h2>
    <div class="controls"><div class="routes-picker" id="rp"></div></div>
    <div class="tablewrap" id="slots"></div>`;
  const cols = (withRoute) => [
    ...(withRoute ? [{ key: "route_id", label: "", cls: "l", html: (r) => S.bullet(r.route_id, "sm") }] : []),
    { key: "label", label: "Trip", cls: "l", html: (r) => `${r.label}<span class="muted"> to ${r.to || "?"}</span>` },
    { key: "trips", label: "Runs", fmt: S.fmt.num },
    { key: "on_sched_rate", label: "On sched", fmt: S.fmt.pct0 },
    { key: "time_saved", label: "Saved", fmt: S.fmt.mmss },
    { key: "median_delay", label: "Med. delay", fmt: S.fmt.mmss, desc: false },
    { key: "tar", label: "TAR", fmt: (x) => S.fmt.num(x, 1) },
  ];
  const obj = (rows) => rows.map((r) => Object.fromEntries(top.cols.map((c, i) => [c, r[i]])));
  S.table(body.querySelector("#best"), cols(true), obj(top.best), { sort: "tar", limit: 12 });
  S.table(body.querySelector("#worst"), cols(true), obj(top.worst), { sort: "tar", desc: false, limit: 12 });
  const rp = body.querySelector("#rp");
  rp.innerHTML = routes.map((r) => `<button data-r="${r}" class="${r === route ? "on" : ""}">${S.bullet(r)}</button>`).join("");
  const draw = async () => {
    const blob = await S.load(`slots/${route}.json`);
    const rows = S.objs(blob).filter((r) => r.qualified);
    S.table(body.querySelector("#slots"), cols(false).concat([{ key: "rank_line", label: "Line rank", desc: false,
      fmt: (x) => x == null ? "–" : S.fmt.num(x) }]), rows, { sort: "tar" });
  };
  rp.querySelectorAll("button").forEach((b) => (b.onclick = () => {
    route = b.dataset.r; rp.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
    history.replaceState(null, "", `#/savant?v=trips&route=${route}`); draw();
  }));
  draw();
}

/* ------------------------------------------------------------------ lines */
async function linesView(body) {
  const blob = await S.load("lines.json");
  const rows = S.objs(blob);
  body.innerHTML = `
    <p class="note">Line-level numbers over the whole window. Completion is the share of scheduled trips that ran start to end.
    TAR/day is on-schedule trips per day above a replacement-level line (the 20th percentile).</p>
    <div class="tablewrap" id="lines" style="max-height:none"></div>`;
  const spark = (r) => {
    const tr = (blob.trend[r.route_id] || []).map((d) => d[1]);
    if (tr.length < 2) return "";
    const w = 90, h = 22, x = (i) => (i / (tr.length - 1)) * w, y = (v) => h - 2 - v * (h - 4);
    const p = tr.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");
    return `<svg width="${w}" height="${h}"><path d="${p}" fill="none" stroke="var(--ink-2)" stroke-width="1.5"/></svg>`;
  };
  S.table(body.querySelector("#lines"), [
    { key: "route_id", label: "Line", cls: "l", html: (r) => S.bullet(r.route_id) },
    { key: "avg_trips_Weekday", label: "Trips/wkday", fmt: S.fmt.num },
    { key: "avg_trips_Saturday", label: "Sat", fmt: S.fmt.num },
    { key: "avg_trips_Sunday", label: "Sun", fmt: S.fmt.num },
    { key: "on_sched_rate", label: "On sched", fmt: S.fmt.pct },
    { key: "median_delay", label: "Med. delay", fmt: S.fmt.mmss, desc: false },
    { key: "p90_delay", label: "90th pct", fmt: S.fmt.mmss, desc: false },
    { key: "completion", label: "Completion", fmt: S.fmt.pct },
    { key: "tar_per_day", label: "TAR/day", fmt: (x) => S.fmt.num(x, 1) },
    { key: "trend", label: "On sched by day", html: spark },
  ], rows, { sort: "tar_per_day", onClick: (r) => (location.hash = `#/trains?route=${r.route_id}`) });
}
