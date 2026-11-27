/* Hash router and boot. */
(async function () {
  const [meta, net] = await Promise.all([S.load("meta.json"), S.load("network.json")]);
  Object.entries(net.lines).forEach(([r, L]) => (S.lineInfo[r] = L));
  S.cfg = meta.definitions;
  document.getElementById("sim-banner").hidden = !meta.simulated;
  document.getElementById("foot").innerHTML =
    `${meta.dates.length} service days, ${S.fmt.date(meta.dates[0])} to ${S.fmt.date(meta.dates[meta.dates.length - 1])} · built ${meta.generated.replace("T", " ")}
     · data from the MTA · <a href="#/about">method</a>`;

  async function route() {
    const [path, qs] = (location.hash.slice(2) || "savant").split("?");
    const page = S.pages[path] ? path : "savant";
    document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("on", a.getAttribute("href") === `#/${page}`));
    const root = document.getElementById("app");
    root.innerHTML = `<div class="loading">Loading…</div>`;
    S.untip();
    try {
      await S.pages[page](root, new URLSearchParams(qs || ""));
    } catch (e) {
      console.error(e);
      root.innerHTML = `<div class="empty">Something went wrong loading this page: ${e.message}</div>`;
    }
  }
  let last = "";
  window.addEventListener("hashchange", () => {
    // replaceState updates inside a page don't fire hashchange; only real navigation re-renders
    const p = location.hash.split("?")[0];
    if (p !== last || location.hash.includes("v=")) { last = p; route(); }
  });
  last = location.hash.split("?")[0];
  route();
})();
