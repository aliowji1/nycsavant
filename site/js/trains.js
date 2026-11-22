/* Trains: Marey diagram with live position strip, aligned trips, trips per day. */
S.pages = S.pages || {};

const WINDOWS = { am: [5.5, 10.5, "Morning rush"], mid: [10, 15.5, "Midday"], pm: [15, 20.5, "Evening rush"],
  night: [20, 27, "Night"], all: [4, 27, "Whole day"] };

S.pages.trains = async (root, params) => {
  const [meta, net, lines] = await Promise.all([S.load("meta.json"), S.load("network.json"), S.load("lines.json")]);
  const routes = S.sortRoutes(Object.keys(net.lines));
  let route = params.get("route") || "A";
  if (!routes.includes(route)) route = routes[0];
  let date = params.get("date") || meta.dates.filter((d) => new Date(d + "T12:00").getDay() % 6 !== 0).slice(-1)[0] || meta.dates.slice(-1)[0];
  let win = params.get("w") || "am";
  root.innerHTML = `
    <div class="kicker">The trains</div>
    <h1>A day on the line, one train at a time.</h1>
    <p class="lede">Each line is one train. Stations run left to right and time runs down the page, so a steep line
    is a slow train and a gap between lines is a long wait. This is a <b>Marey diagram</b>, named for Étienne-Jules Marey,
    who popularized it for French railway timetables in the 1880s. Hover to see where every train was at that moment.</p>
    <div class="controls">
      <div class="routes-picker" id="t-rp"></div>
    </div>
    <div class="controls">
      <label>Day<select id="t-date">${meta.dates.map((d) => `<option value="${d}" ${d === date ? "selected" : ""}>${S.fmt.date(d)}</option>`).join("")}</select></label>
      <label>Hours<select id="t-win">${Object.entries(WINDOWS).map(([k, v]) => `<option value="${k}" ${k === win ? "selected" : ""}>${v[2]}</option>`).join("")}</select></label>
    </div>
    <div class="card" style="padding:10px 6px 6px">
      <div id="t-strip" style="position:sticky;top:56px;background:var(--surface-1);z-index:5"></div>
      <div id="t-marey" class="chart scrollx"></div>
    </div>
    <h2>Every trip, lined up at the start</h2>
    <p class="note">The same trips, each shifted to start at zero. The spread at the bottom is the gap between the fastest and slowest
    run. Darker lines started later in the day. Click a line for its stop-by-stop times.</p>
    <div class="grid2" id="t-aligned"></div>
    <div id="t-stops"></div>
    <h2>Trips per day</h2>
    <p class="note">Average number of trips that ran per service day.</p>
    <div class="tablewrap" id="t-count" style="max-height:none"></div>`;

  const rp = root.querySelector("#t-rp");
  rp.innerHTML = routes.map((r) => `<button data-r="${r}" class="${r === route ? "on" : ""}">${S.bullet(r)}</button>`).join("");
  const update = () => {
    history.replaceState(null, "", `#/trains?route=${route}&date=${date}&w=${win}`);
    draw();
  };
  rp.querySelectorAll("button").forEach((b) => (b.onclick = () => {
    route = b.dataset.r; rp.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b)); update();
  }));
  root.querySelector("#t-date").onchange = (e) => { date = e.target.value; update(); };
  root.querySelector("#t-win").onchange = (e) => { win = e.target.value; update(); };

  const lt = S.objs(lines);
  S.table(root.querySelector("#t-count"), [
    { key: "route_id", label: "Line", cls: "l", html: (r) => S.bullet(r.route_id) },
    { key: "avg_trips_Weekday", label: "Weekday", fmt: S.fmt.num },
    { key: "avg_trips_Saturday", label: "Saturday", fmt: S.fmt.num },
    { key: "avg_trips_Sunday", label: "Sunday", fmt: S.fmt.num },
    { key: "median_runtime", label: "Median trip", fmt: S.fmt.mins },
  ], lt.map((r) => ({ ...r, order: S.routeOrder.indexOf(r.route_id) })), { sort: "avg_trips_Weekday", rank: false,
    onClick: (r) => { route = r.route_id; rp.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x.dataset.r === route)); update(); window.scrollTo({ top: 0, behavior: "smooth" }); } });

  async function draw() {
    const day = await S.load(`marey/${date}.json`);
    const L = net.lines[route];
    const order = (L.dirs["1"] || Object.values(L.dirs)[0]);
    const trips = day[route] || [];
    marey(root.querySelector("#t-marey"), root.querySelector("#t-strip"), L, order, trips, WINDOWS[win], net);
    aligned(root.querySelector("#t-aligned"), root.querySelector("#t-stops"), L, order, trips, net);
  }
  draw();
};

function stationName(net) {
  const m = new Map(net.stations.map((s) => [s[0], s[1]]));
  return (id) => m.get(id) || id;
}

function marey(el, stripEl, L, order, trips, win, net) {
  const name = stationName(net);
  const W = Math.max(760, el.clientWidth), m = { t: 4, r: 70, b: 10, l: 52 };
  const [h0, h1] = win, pxPerHour = h1 - h0 > 10 ? 150 : 230;
  const H = (h1 - h0) * pxPerHour + m.t + m.b;
  const x = d3.scaleLinear([0, order.dist_m[order.dist_m.length - 1]], [m.l, W - m.r]);
  const y = d3.scaleLinear([h0 * 3600, h1 * 3600], [m.t, H - m.b]);
  const sx = (i) => x(order.dist_m[i]);
  const color = L.color;

  // position strip with station labels (sticky)
  const SH = 156;
  const strip = d3.select(stripEl).html("").append("svg").attr("width", W).attr("height", SH);
  const lab = strip.append("g");
  let lastX = -99;
  order.stations.forEach((s, i) => {
    if (sx(i) - lastX < 11 && i !== order.stations.length - 1) return;   // skip labels that would collide
    lastX = sx(i);
    lab.append("text").attr("transform", `translate(${sx(i)},${SH - 44}) rotate(-50)`).attr("font-size", 10.5).text(name(s));
    lab.append("line").attr("x1", sx(i)).attr("x2", sx(i)).attr("y1", SH - 38).attr("y2", SH - 6).attr("stroke", "var(--grid)");
  });
  strip.append("line").attr("x1", sx(0)).attr("x2", sx(order.stations.length - 1)).attr("y1", SH - 22).attr("y2", SH - 22)
    .attr("stroke", color).attr("stroke-width", 4).attr("stroke-linecap", "round");
  const clock = strip.append("text").attr("x", 4).attr("y", SH - 18).attr("font-weight", 700).attr("font-size", 13).attr("fill", "var(--ink)");
  const dots = strip.append("g");
  const lastName = name(order.stations[order.stations.length - 1]), firstName = name(order.stations[0]);
  strip.append("text").attr("x", 4).attr("y", 12).attr("fill", "var(--ink-3)").attr("font-size", 11)
    .text(`▲ toward ${firstName}   ▼ toward ${lastName}`);

  const svg = d3.select(el).html("").append("svg").attr("width", W).attr("height", H);
  svg.append("g").selectAll("line").data(order.stations).join("line")
    .attr("x1", (_, i) => sx(i)).attr("x2", (_, i) => sx(i)).attr("y1", m.t).attr("y2", H - m.b).attr("stroke", "var(--grid)");
  const ticks = d3.range(Math.ceil(h0 * 2) / 2, h1 + 0.01, 0.5).map((h) => h * 3600);
  svg.append("g").selectAll("text").data(ticks).join("text").attr("x", m.l - 8).attr("y", (d) => y(d) + 4)
    .attr("text-anchor", "end").attr("fill", "var(--ink-3)").text((d) => S.fmt.clock(d));
  svg.append("g").selectAll("line").data(ticks).join("line").attr("x1", m.l).attr("x2", W - m.r)
    .attr("y1", (d) => y(d)).attr("y2", (d) => y(d)).attr("stroke", "var(--grid)").attr("stroke-dasharray", "2,3");

  const vis = trips.filter((t) => t[2][t[2].length - 1] >= h0 * 3600 && t[2][0] <= h1 * 3600);
  const line = d3.line().x((d) => sx(d[0])).y((d) => y(d[1]));
  const paths = svg.append("g").attr("fill", "none").selectAll("path").data(vis).join("path")
    .attr("d", (t) => line(t[1].map((s, i) => [s, t[2][i]])))
    .attr("stroke", color).attr("stroke-width", 1.4).attr("stroke-opacity", (t) => (t[0] === 0 ? 0.55 : 0.95));
  if (!vis.length) svg.append("text").attr("x", W / 2).attr("y", 60).attr("text-anchor", "middle").text("No trips on this line in these hours.");

  const cross = svg.append("line").attr("x1", m.l).attr("x2", W - m.r).attr("stroke", "var(--ink)").attr("stroke-width", 1).style("display", "none");
  const hit = svg.append("g").attr("fill", "none").attr("stroke", "transparent").attr("stroke-width", 9).selectAll("path").data(vis).join("path")
    .attr("d", (t) => line(t[1].map((s, i) => [s, t[2][i]])));

  function positions(t) {
    dots.selectAll("*").remove();
    clock.text(S.fmt.clock(t));
    for (const tr of trips) {
      const ts = tr[2];
      if (t < ts[0] || t > ts[ts.length - 1]) continue;
      const k = d3.bisectRight(ts, t);
      const i0 = Math.max(0, k - 1), i1 = Math.min(ts.length - 1, k);
      const f = ts[i1] === ts[i0] ? 0 : (t - ts[i0]) / (ts[i1] - ts[i0]);
      const px = sx(tr[1][i0]) + f * (sx(tr[1][i1]) - sx(tr[1][i0]));
      const up = tr[0] === 0;
      dots.append("path").attr("transform", `translate(${px},${SH - 22 + (up ? -9 : 9)})`)
        .attr("d", up ? "M0,-5L4.5,3H-4.5Z" : "M0,5L4.5,-3H-4.5Z").attr("fill", "var(--ink)").attr("stroke", "var(--surface-1)").attr("stroke-width", 1.5);
    }
  }
  svg.on("mousemove", (ev) => {
    const [, py] = d3.pointer(ev);
    const t = y.invert(py);
    cross.style("display", null).attr("y1", py).attr("y2", py);
    positions(t);
  }).on("mouseleave", () => { cross.style("display", "none"); S.untip(); });
  hit.on("mouseenter", function (ev, t) {
    paths.filter((p) => p === t).attr("stroke-width", 3).raise();
  }).on("mousemove", (ev, t) => {
    const a = t[2][0], b = t[2][t[2].length - 1];
    S.tip(`${S.bullet(L.route, "sm")} <b>${t[3]}</b><br>${name(order.stations[t[1][0]])} ${S.fmt.clock(a)} → ${name(order.stations[t[1][t[1].length - 1]])} ${S.fmt.clock(b)}<br>${Math.round((b - a) / 60)} min, ${t[1].length} stops`, ev);
  }).on("mouseleave", (ev, t) => { paths.filter((p) => p === t).attr("stroke-width", 1.4); });
  positions((h0 + 1) * 3600);
}

function aligned(el, stopsEl, L, order, trips, net) {
  const name = stationName(net);
  el.innerHTML = "";
  stopsEl.innerHTML = "";
  const hours = d3.extent(trips, (t) => t[2][0]);
  const seq = d3.scaleSequential([hours[0] || 0, hours[1] || 1], d3.interpolateRgb("#b7d3f6", "#0d366b"));
  [1, 0].forEach((dir) => {
    const ts = trips.filter((t) => t[0] === dir);
    const box = S.el(`<div class="card"><h3 style="margin-top:0">Toward ${name(order.stations[dir === 1 ? order.stations.length - 1 : 0])}</h3><div class="chart"></div></div>`);
    el.appendChild(box);
    const c = box.querySelector(".chart");
    const W = Math.max(320, c.clientWidth || 520), H = 300, m = { t: 8, r: 10, b: 30, l: 40 };
    const dmax = order.dist_m[order.dist_m.length - 1];
    const ydist = (i) => (dir === 1 ? order.dist_m[i] : dmax - order.dist_m[i]);
    const x = d3.scaleLinear([0, d3.max(ts, (t) => t[2][t[2].length - 1] - t[2][0]) / 60 || 60], [m.l, W - m.r]).nice();
    const y = d3.scaleLinear([0, dmax / 1000], [m.t, H - m.b]);
    const svg = d3.select(c).append("svg").attr("width", W).attr("height", H);
    svg.append("g").attr("class", "gridline").attr("transform", `translate(0,${H - m.b})`).call(d3.axisBottom(x).ticks(6).tickSize(-(H - m.t - m.b)).tickFormat(""));
    svg.append("g").attr("class", "axis").attr("transform", `translate(0,${H - m.b})`).call(d3.axisBottom(x).ticks(6).tickFormat((d) => `${d} min`));
    svg.append("g").attr("class", "axis").attr("transform", `translate(${m.l},0)`).call(d3.axisLeft(y).ticks(5).tickFormat((d) => `${d} km`));
    const line = d3.line().x((d) => x(d[0])).y((d) => y(d[1]));
    const p = svg.append("g").attr("fill", "none").selectAll("path").data(ts).join("path")
      .attr("d", (t) => line(t[1].map((s, i) => [(t[2][i] - t[2][0]) / 60, ydist(s) / 1000])))
      .attr("stroke", (t) => seq(t[2][0])).attr("stroke-width", 1.2).attr("stroke-opacity", 0.8);
    svg.append("g").attr("fill", "none").attr("stroke", "transparent").attr("stroke-width", 8).selectAll("path").data(ts).join("path")
      .attr("d", (t) => line(t[1].map((s, i) => [(t[2][i] - t[2][0]) / 60, ydist(s) / 1000]))).style("cursor", "pointer")
      .on("mouseenter", (_, t) => p.filter((q) => q === t).attr("stroke", "var(--series-2)").attr("stroke-width", 2.5).raise())
      .on("mousemove", (ev, t) => S.tip(`<b>${t[3]}</b><br>Left ${S.fmt.clock(t[2][0])}, ${Math.round((t[2][t[2].length - 1] - t[2][0]) / 60)} min`, ev))
      .on("mouseleave", (_, t) => { S.untip(); p.filter((q) => q === t).attr("stroke", seq(t[2][0])).attr("stroke-width", 1.2); })
      .on("click", (_, t) => {
        stopsEl.innerHTML = `<div class="card" style="margin-top:14px"><h3 style="margin-top:0">${S.bullet(L.route, "sm")} ${t[3]}</h3>
          <div class="tablewrap" style="max-height:320px"><table class="t"><thead><tr><th class="l">Station</th><th>Arrived</th><th>Since start</th></tr></thead><tbody>
          ${t[1].map((s, i) => `<tr><td class="l">${name(order.stations[s])}</td><td>${S.fmt.clock(t[2][i])}</td><td>${Math.round((t[2][i] - t[2][0]) / 60)} min</td></tr>`).join("")}
          </tbody></table></div></div>`;
      });
  });
  el.insertAdjacentHTML("beforeend", `<div class="legend" style="grid-column:1/-1">Departure time
    <span class="ramp" style="background:linear-gradient(90deg,#b7d3f6,#0d366b)"></span>${S.fmt.clock(hours[0] || 0)} to ${S.fmt.clock(hours[1] || 0)}${(hours[1] || 0) >= 86400 ? " (after midnight)" : ""}</div>`);
}
