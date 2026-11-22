/* Riders: hourly entries heatmap, station table + map, per-station profiles. */
S.pages = S.pages || {};

const KIND = { home: ["Home", "var(--series-1)", "busiest in the morning: people leaving home"],
  work: ["Work", "var(--series-2)", "busiest in the evening: people heading home from work"],
  "all-day": ["All day", "#1baf7a", "busy at both peaks or all day"] };

S.pages.riders = async (root, params) => {
  let R;
  try { R = await S.load("ridership.json"); } catch (e) { R = null; }
  if (!R) { root.innerHTML = `<h1>Riders</h1><p class="empty">No ridership data yet. Run <code>python -m savant ridership</code>.</p>`; return; }
  const st = R.stations;
  root.innerHTML = `
    <div class="kicker">The people</div>
    <h1>Who rides, and when</h1>
    <p class="lede">Each row is a day and each column an hour, shaded by how many people entered the subway. The morning and evening
    rush show up as two dark bands on weekdays and disappear on weekends.</p>
    <div class="card"><div class="chart" id="r-heat"></div>
      <div class="legend">Entries per hour <span class="ramp" style="background:linear-gradient(90deg,#cde2fb,#0d366b)"></span></div></div>
    <h2>Home stations and work stations</h2>
    <p class="note">A station whose entries peak in the morning is where people live; one that peaks in the evening is where they work.
    Exits come from the MTA's origin-destination estimates, which are modeled, so treat them as typical patterns rather than counts.</p>
    <div class="controls"><div class="seg" id="r-kind"><button data-k="" class="on">All</button>
      ${Object.entries(KIND).map(([k, v]) => `<button data-k="${k}">${v[0]}</button>`).join("")}</div>
      <label>Find<input type="search" id="r-q" placeholder="Station name"></label></div>
    <div class="grid2">
      <div class="card"><div class="chart" id="r-map"></div>
        <div class="legend">${Object.values(KIND).map((v) => `<span><i style="background:${v[1]};border-radius:50%"></i>${v[0]}</span>`).join("")}<span>Size = weekday entries</span></div></div>
      <div class="tablewrap" id="r-table" style="max-height:600px"></div>
    </div>
    <div id="r-detail"></div>`;

  heat(root.querySelector("#r-heat"), R.days, R.system, (d) => S.fmt.date(d));

  // map
  const mel = root.querySelector("#r-map");
  const MW = Math.max(320, mel.clientWidth || 520), MH = 560;
  const proj = S.projection(MW, MH, st);
  const svg = d3.select(mel).append("svg").attr("width", MW).attr("height", MH);
  const net = await S.load("network.json");
  Object.values(net.lines).forEach((L) => {
    const sh = (L.dirs["1"] || Object.values(L.dirs)[0]).shape;
    if (sh && sh.length) svg.append("path").attr("d", d3.line()(sh.map((p) => proj(p)))).attr("fill", "none")
      .attr("stroke", "var(--grid)").attr("stroke-width", 3).attr("stroke-linecap", "round");
  });
  const r = d3.scaleSqrt([0, d3.max(st, (s) => s.wd)], [0, 14]);
  const circ = svg.append("g").selectAll("circle").data(st.slice().sort((a, b) => b.wd - a.wd)).join("circle")
    .attr("cx", (s) => proj([s.lon, s.lat])[0]).attr("cy", (s) => proj([s.lon, s.lat])[1]).attr("r", (s) => Math.max(2, r(s.wd)))
    .attr("fill", (s) => KIND[s.kind][1]).attr("fill-opacity", 0.75).attr("stroke", "var(--surface-1)").attr("stroke-width", 1)
    .style("cursor", "pointer")
    .on("mousemove", (ev, s) => { S.tip(`<b>${s.name}</b><br>${S.fmt.num(s.wd)} weekday entries<br>${KIND[s.kind][0]} station`, ev); hl(s.id); })
    .on("mouseleave", () => { S.untip(); hl(null); })
    .on("click", (_, s) => detail(s));

  let kind = "", q = "";
  const cols = [
    { key: "name", label: "Station", cls: "l wrap" },
    { key: "kind", label: "Type", cls: "l", html: (s) => `<span class="chip" style="box-shadow:inset 3px 0 0 ${KIND[s.kind][1]}">${KIND[s.kind][0]}</span>` },
    { key: "wd", label: "Weekday", fmt: S.fmt.num },
    { key: "we", label: "Weekend", fmt: S.fmt.num },
    { key: "ratio", label: "AM/PM", fmt: (x) => S.fmt.num(x, 2), title: "Weekday entries 6-10a divided by 4-8p" },
  ];
  const tbl = S.table(root.querySelector("#r-table"), cols, st, { sort: "wd", onClick: detail });
  const tbody = root.querySelector("#r-table tbody");
  tbody.addEventListener("mouseover", (e) => { const tr = e.target.closest("tr"); if (tr) hl(d3.select(tr).datum().id); });
  tbody.addEventListener("mouseleave", () => hl(null));
  function hl(id) {
    circ.attr("stroke", (s) => (s.id === id ? "var(--ink)" : "var(--surface-1)")).attr("stroke-width", (s) => (s.id === id ? 2.5 : 1));
    if (id) circ.filter((s) => s.id === id).raise();
  }
  const filt = () => {
    const rows = st.filter((s) => (!kind || s.kind === kind) && (!q || s.name.toLowerCase().includes(q)));
    tbl.redraw(rows);
    circ.attr("display", (s) => ((!kind || s.kind === kind) && (!q || s.name.toLowerCase().includes(q)) ? null : "none"));
  };
  root.querySelectorAll("#r-kind button").forEach((b) => (b.onclick = () => {
    kind = b.dataset.k; root.querySelectorAll("#r-kind button").forEach((x) => x.classList.toggle("on", x === b)); filt();
  }));
  root.querySelector("#r-q").oninput = (e) => { q = e.target.value.toLowerCase(); filt(); };

  function detail(s) {
    const el = root.querySelector("#r-detail");
    const m = d3.range(R.days.length).map((i) => s.m.slice(i * 24, i * 24 + 24));
    el.innerHTML = `<div class="card" style="margin-top:16px">
      <div class="kicker">${KIND[s.kind][0]} station · ${KIND[s.kind][2]}</div>
      <h2 style="margin:4px 0 10px">${s.name}</h2>
      <div class="grid2">
        <div><h3>Weekdays, average hour</h3><div class="chart" id="r-wd"></div></div>
        <div><h3>Weekends, average hour</h3><div class="chart" id="r-we"></div></div>
      </div>
      <h3>Every hour of every day</h3><div class="chart" id="r-sheat"></div></div>`;
    profile(el.querySelector("#r-wd"), s.nwd, s.xwd);
    profile(el.querySelector("#r-we"), s.nwe, s.xwe);
    heat(el.querySelector("#r-sheat"), R.days, m, (d) => S.fmt.date(d));
    el.scrollIntoView({ behavior: "smooth", block: "start" });
  }
};

function heat(el, days, mat, label) {
  el.innerHTML = "";
  const W = Math.max(600, el.clientWidth), m = { t: 20, r: 8, b: 6, l: 92 }, rh = 16;
  const H = m.t + days.length * rh + m.b;
  const x = d3.scaleBand(d3.range(24), [m.l, W - m.r]).paddingInner(0.08);
  const max = d3.max(mat.flat()) || 1;
  const c = d3.scaleSequential([0, max], d3.interpolateRgb("#cde2fb", "#0d366b"));
  const svg = d3.select(el).append("svg").attr("width", W).attr("height", H);
  svg.append("g").selectAll("text").data(d3.range(0, 24, 3)).join("text").attr("x", (h) => x(h)).attr("y", 12).text((h) => S.fmt.clock(h * 3600));
  days.forEach((d, i) => {
    svg.append("text").attr("x", m.l - 8).attr("y", m.t + i * rh + 11.5).attr("text-anchor", "end").text(label(d));
    svg.append("g").selectAll("rect").data((mat[i] || []).map((v, h) => ({ v, h }))).join("rect")
      .attr("x", (o) => x(o.h)).attr("y", m.t + i * rh).attr("width", x.bandwidth()).attr("height", rh - 2).attr("rx", 2)
      .attr("fill", (o) => c(o.v))
      .on("mousemove", (ev, o) => S.tip(`<b>${label(d)}, ${S.fmt.clock(o.h * 3600)}</b><br>${S.fmt.num(o.v)} entries`, ev))
      .on("mouseleave", S.untip);
  });
}

function profile(el, entries, exits) {
  const W = Math.max(300, el.clientWidth || 460), H = 190, m = { t: 10, r: 52, b: 24, l: 40 };
  const x = d3.scaleLinear([0, 23], [m.l, W - m.r]);
  const y = d3.scaleLinear([0, d3.max([...(entries || []), ...(exits || [])]) || 1], [H - m.b, m.t]).nice();
  const svg = d3.select(el).append("svg").attr("width", W).attr("height", H);
  svg.append("g").attr("class", "gridline").attr("transform", `translate(${m.l},0)`).call(d3.axisLeft(y).ticks(4).tickSize(-(W - m.l - m.r)).tickFormat(""));
  svg.append("g").attr("class", "axis").attr("transform", `translate(${m.l},0)`).call(d3.axisLeft(y).ticks(4).tickFormat(d3.format("~s")));
  svg.append("g").attr("class", "axis").attr("transform", `translate(0,${H - m.b})`).call(d3.axisBottom(x).tickValues([0, 6, 12, 18, 23]).tickFormat((h) => S.fmt.clock(h * 3600)));
  const ser = [["Entries", entries, "var(--series-1)"], ["Exits", exits, "var(--series-2)"]].filter((s) => s[1]);
  const labs = [];
  ser.forEach(([nm, v, col]) => {
    svg.append("path").datum(v).attr("fill", "none").attr("stroke", col).attr("stroke-width", 2)
      .attr("d", d3.line().x((_, i) => x(i)).y((d) => y(d)).curve(d3.curveMonotoneX));
    labs.push({ nm, y: y(v[23]) + 4 });
  });
  labs.sort((a, b) => a.y - b.y);
  for (let i = 1; i < labs.length; i++) labs[i].y = Math.max(labs[i].y, labs[i - 1].y + 13);   // nudge apart
  labs.forEach((l) => svg.append("text").attr("x", x(23) + 6).attr("y", l.y).attr("fill", "var(--ink-2)").text(l.nm));
  const cross = svg.append("line").attr("y1", m.t).attr("y2", H - m.b).attr("stroke", "var(--ink-3)").style("display", "none");
  svg.append("rect").attr("x", m.l).attr("y", m.t).attr("width", W - m.l - m.r).attr("height", H - m.t - m.b).attr("fill", "transparent")
    .on("mousemove", (ev) => {
      const h = Math.round(x.invert(d3.pointer(ev)[0]));
      cross.style("display", null).attr("x1", x(h)).attr("x2", x(h));
      S.tip(`<b>${S.fmt.clock(h * 3600)}</b>` + ser.map(([nm, v]) => `<br>${nm}: ${S.fmt.num(v[h])}`).join(""), ev);
    }).on("mouseleave", () => { cross.style("display", "none"); S.untip(); });
  if (ser.length > 1) el.insertAdjacentHTML("beforeend", `<div class="legend">${ser.map(([nm, , c]) => `<span><i style="background:${c}"></i>${nm}</span>`).join("")}</div>`);
}
