(() => {
  "use strict";

  const $ = (s) => document.querySelector(s);
  const nf = (d) => new Intl.NumberFormat("nb-NO", { minimumFractionDigits: d, maximumFractionDigits: d });
  const pct = (x, d = 1) => (x == null ? "–" : new Intl.NumberFormat("nb-NO", { style: "percent", minimumFractionDigits: d, maximumFractionDigits: d }).format(x));
  const num = (x, d = 2) => (x == null ? "–" : nf(d).format(x));
  const signed = (x, d = 1) => (x == null ? "–" : (x > 0 ? "+" : x < 0 ? "−" : "") + pct(Math.abs(x), d));
  const dateLong = (s) => s ? new Date(s + "T12:00:00").toLocaleDateString("nb-NO", { day: "numeric", month: "long", year: "numeric" }) : "–";
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  let DATA = null;
  let method = null;
  let period = "live";
  const charts = {};

  function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
  function palette() {
    return { ink: css("--ink"), ink2: css("--ink-2"), line: css("--line"), fjord: css("--fjord"), soft: css("--fjord-soft"),
             slate: css("--slate"), amber: css("--amber"), red: css("--red"), panel: css("--panel") };
  }

  function baseOptions(P) {
    return {
      responsive: true, maintainAspectRatio: false, animation: false,
      plugins: { legend: { display: false }, tooltip: { backgroundColor: P.ink, titleColor: P.panel, bodyColor: P.panel, padding: 10, cornerRadius: 6 } },
      scales: {},
    };
  }
  function axis(P, extra = {}) {
    return Object.assign({ grid: { color: P.line, drawTicks: false }, border: { display: false },
      ticks: { color: P.ink2, padding: 6, font: { family: css("--font"), size: 12 } } }, extra);
  }
  function mount(id, cfg) {
    if (charts[id]) charts[id].destroy();
    charts[id] = new Chart(document.getElementById(id), cfg);
  }

  // ───────────── oppstart ─────────────
  fetch("data/resultat.json", { cache: "no-store" })
    .then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); })
    .then((d) => { DATA = d; init(); })
    .catch(() => {
      $("#status").textContent = "";
      const e = $("#error");
      e.hidden = false;
      e.innerHTML = "<h2>Fant ingen resultater</h2><p>Filen data/resultat.json mangler. Den lages av GitHub Actions. Åpne fanen Actions i repoet og sjekk at siste kjøring av «Oppdater portefølje» ble fullført.</p>";
    });

  function init() {
    if (typeof Chart === "undefined") {
      $("#error").hidden = false;
      $("#error").innerHTML = "<h2>Grafene kunne ikke lastes</h2><p>Chart.js ble ikke hentet fra cdn.jsdelivr.net. Sjekk nettforbindelsen og last siden på nytt.</p>";
    }
    document.querySelectorAll('[data-f="indeks"]').forEach((el) => (el.textContent = DATA.indeks));
    $("#demoBanner").hidden = !DATA.demo;
    $("#status").innerHTML =
      `<span>Kurser per <b>${dateLong(DATA.siste_kursdato)}</b></span>` +
      `<span>Neste rebalansering: <b>${esc(DATA.neste_rebalansering.charAt(0).toLowerCase() + DATA.neste_rebalansering.slice(1))}</b></span>` +
      `<span>Risikofri rente: <b>${pct(DATA.risikofri.rente, 2)}</b>, ${esc(DATA.risikofri.kilde)}</span>`;

    const sw = $("#methodSwitch");
    const keys = [DATA.hovedmetode, ...Object.keys(DATA.metoder).filter((k) => k !== DATA.hovedmetode)];
    sw.innerHTML = keys.map((k) => `<button role="radio" data-m="${k}">${esc(DATA.metoder[k].navn)}${k === DATA.hovedmetode ? "<small>hovedmetode</small>" : ""}</button>`).join("");
    sw.addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) select(b.dataset.m); });
    sw.addEventListener("keydown", (e) => {
      if (!["ArrowLeft", "ArrowRight"].includes(e.key)) return;
      const i = keys.indexOf(method); const n = keys[(i + (e.key === "ArrowRight" ? 1 : keys.length - 1)) % keys.length];
      select(n); sw.querySelector(`[data-m="${n}"]`).focus();
    });

    $("#perfTabs").addEventListener("click", (e) => {
      const b = e.target.closest("button"); if (!b) return;
      period = b.dataset.p;
      document.querySelectorAll("#perfTabs button").forEach((x) => x.setAttribute("aria-selected", String(x === b)));
      renderPerf();
    });

    const m = new URLSearchParams(location.search).get("metode");
    $("#app").hidden = false;
    select(DATA.metoder[m] ? m : DATA.hovedmetode);
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => select(method));
  }

  function select(m) {
    method = m;
    document.querySelectorAll("#methodSwitch button").forEach((b) => {
      const on = b.dataset.m === m;
      b.setAttribute("aria-checked", String(on)); b.tabIndex = on ? 0 : -1;
    });
    const M = DATA.metoder[m];
    if (period === "live" && !M.live.serie) period = "backtest";
    document.querySelectorAll("#perfTabs button").forEach((x) => x.setAttribute("aria-selected", String(x.dataset.p === period)));
    renderFront(M); renderNow(M); renderHoldings(M); renderChanges(M); renderPerf(); renderYears(M); renderUniverse(); renderMethod(M);
  }

  // ───────────── effisient front ─────────────
  function renderFront(M) {
    const P = palette();
    const F = M.front;
    if (!F || !F.kurve) return;
    const pts = (arr) => arr.map((p) => ({ x: p.vol * 100, y: p.avk * 100, n: p.navn, t: p.ticker }));
    const held = F.aksjer.filter((a) => a.i_portefolje);
    const other = F.aksjer.filter((a) => !a.i_portefolje);
    const maxX = Math.max(...F.aksjer.map((a) => a.vol), ...F.kurve.map((k) => k.vol)) * 100 * 1.05;
    const ds = [];
    if (F.tangent) {
      const s = (F.tangent.avk - F.rf) / F.tangent.vol;
      ds.push({ label: "Kapitalmarkedslinjen", type: "line", data: [{ x: 0, y: F.rf * 100 }, { x: maxX, y: (F.rf + s * maxX / 100) * 100 }],
        borderColor: P.slate, borderDash: [5, 5], borderWidth: 1.5, pointRadius: 0, tooltip: false });
    }
    ds.push({ label: "Effisient front", type: "line", data: pts(F.kurve), borderColor: P.fjord, borderWidth: 3, pointRadius: 0, tension: 0.25 });
    ds.push({ label: "Andre aksjer", data: pts(other), backgroundColor: P.slate + "88", pointRadius: 3.5, pointHoverRadius: 6 });
    ds.push({ label: "Aksjer i porteføljen", data: pts(held), backgroundColor: P.fjord, pointRadius: 5, pointHoverRadius: 7 });
    if (F.marked) ds.push({ label: "Markedsvektet univers", data: [{ x: F.marked.vol * 100, y: F.marked.avk * 100, n: "Markedsvektet univers" }],
      backgroundColor: P.ink, pointStyle: "rectRot", pointRadius: 7, pointHoverRadius: 9 });
    if (F.portefolje) ds.push({ label: "Porteføljen", data: [{ x: F.portefolje.vol * 100, y: F.portefolje.avk * 100, n: "Porteføljen" }],
      backgroundColor: "transparent", borderColor: P.amber, borderWidth: 4, pointRadius: 10, pointHoverRadius: 12 });

    const o = baseOptions(P);
    o.scales = {
      x: axis(P, { type: "linear", min: 0, max: Math.ceil(maxX / 5) * 5, title: { display: true, text: "Forventet volatilitet (årlig)", color: P.ink2 },
        ticks: { ...axis(P).ticks, callback: (v) => v + " %" } }),
      y: axis(P, { title: { display: true, text: "Forventet avkastning (årlig)", color: P.ink2 }, ticks: { ...axis(P).ticks, callback: (v) => v + " %" } }),
    };
    o.plugins.tooltip.callbacks = {
      title: (items) => items[0].raw.n || items[0].dataset.label,
      label: (it) => `Avkastning ${nf(1).format(it.raw.y)} %, volatilitet ${nf(1).format(it.raw.x)} %`,
    };
    o.plugins.tooltip.filter = (it) => it.dataset.label !== "Kapitalmarkedslinjen";
    mount("frontChart", { type: "scatter", data: { datasets: ds }, options: o });

    $("#frontLegend").innerHTML = [
      `<li><i class="line" style="background:${P.fjord}"></i>Effisient front</li>`,
      `<li><i class="ring"></i>Porteføljen</li>`,
      `<li><i style="background:${P.fjord}"></i>Aksjer i porteføljen</li>`,
      `<li><i style="background:${P.slate}88"></i>Andre aksjer</li>`,
      `<li><i class="diamond" style="background:${P.ink}"></i>Markedsvektet univers (≈ ${esc(DATA.indeks)})</li>`,
      F.tangent ? `<li><i class="dash"></i>Kapitalmarkedslinjen</li>` : "",
    ].join("");
  }

  // ───────────── porteføljen nå ─────────────
  function renderNow(M) {
    const p = M.portefolje, f = M.front.portefolje;
    const when = p.beslutningsdato ? dateLong(p.beslutningsdato) : null;
    $("#nowSummary").innerHTML = when
      ? `${p.antall} aksjer, valgt ${when}.` + (p.handel_venter ? " Handelen gjennomføres til sluttkurs neste børsdag." : "")
      : "Ingen portefølje ennå. Den lages ved første kjøring.";
    const rows = [];
    if (f) {
      rows.push(["Forventet avkastning", pct(f.avk), "per år, dagens estimat"]);
      rows.push(["Forventet volatilitet", pct(f.vol), "per år"]);
      rows.push(["Sharpe-ratio", num(f.sharpe), `mot ${pct(DATA.risikofri.rente, 2)} risikofri`]);
    }
    if (p.forste_kjop) rows.push(["Omsetning ved siste rebalansering", "Første kjøp", `grensen på ${pct(DATA.innstillinger.maks_omsetning, 0)} gjelder fra neste måned`]);
    else rows.push(["Omsetning ved siste rebalansering", pct(p.omsetning, 0), `grense ${pct(DATA.innstillinger.maks_omsetning, 0)}`]);
    $("#nowStats").innerHTML = rows.map(([a, b, c]) => `<dt>${a}</dt><dd>${b}<small>${c}</small></dd>`).join("");
    const notes = [...p.merknader];
    notes.push("Estimatene oppdateres hver børsdag. Vektene endres bare ved månedlig rebalansering.");
    $("#nowNotes").textContent = notes.join(" ");
  }

  // ───────────── beholdning ─────────────
  function renderHoldings(M) {
    const p = M.portefolje, S = DATA.innstillinger;
    const maxW = Math.max(S.maks_vekt, ...p.beholdning.map((h) => h.navekt || 0));
    $("#holdSub").textContent = `Maks ${pct(S.maks_vekt, 0)} per selskap og ${pct(S.maks_sektorvekt, 0)} per sektor. «Dager å selge» forutsetter en portefølje på ${nf(0).format(S.portefoljestorrelse_nok)} kr og at du står for høyst ${pct(S.deltakelsesgrad, 0)} av dagsomsetningen.`;
    const bar = (w) => `<div class="wbar">${pct(w)}<span class="bar" style="width:${Math.round((w / maxW) * 60)}px"></span></div>`;
    $("#holdTable tbody").innerHTML = p.beholdning.map((h) => `
      <tr class="${h.malvekt ? "" : "out"}">
        <td><span class="name">${esc(h.navn)}</span><span class="tk">${esc(h.ticker.replace(".OL", ""))}</span>${h.i_univers ? "" : ' <span class="flag">ikke lenger likvid nok</span>'}</td>
        <td>${esc(h.sektor)}</td>
        <td class="num wcell">${bar(h.malvekt || 0)}</td>
        <td class="num">${pct(h.navekt)}</td>
        <td class="num">${pct(h.forventet_avk)}</td>
        <td class="num">${pct(h.vol, 0)}</td>
        <td class="num">${h.omsetning_mnok == null ? "–" : nf(h.omsetning_mnok < 10 ? 1 : 0).format(h.omsetning_mnok) + " MNOK"}</td>
        <td class="num">${h.dager_aa_selge == null ? "–" : h.dager_aa_selge < 0.1 ? "under 0,1" : num(h.dager_aa_selge, 1)}</td>
      </tr>`).join("") || `<tr><td colspan="8">Ingen beholdning ennå.</td></tr>`;

    const lim = S.maks_sektorvekt;
    const scale = Math.max(lim * 1.15, ...p.sektorer.map((s) => s.vekt));
    $("#sectorBars").innerHTML = p.sektorer.map((s) => `
      <li><div class="row"><span>${esc(s.sektor)}</span><span>${pct(s.vekt)}</span></div>
      <div class="track"><div class="fill" style="width:${(s.vekt / scale) * 100}%"></div><div class="limit" style="left:${(lim / scale) * 100}%" title="Grense ${pct(lim, 0)}"></div></div></li>`).join("");
    $("#sectorNote").textContent = `Den svarte streken markerer sektorgrensen på ${pct(lim, 0)}.`;
  }

  // ───────────── endringer ─────────────
  function renderChanges(M) {
    const c = M.portefolje.endringer;
    const p = M.portefolje;
    $("#chgSub").textContent = p.beslutningsdato ? `Rebalansert ${dateLong(p.beslutningsdato)}. Endringer under 0,5 prosentpoeng er utelatt.` : "";
    const item = (x) => {
      const d = x.til - x.fra;
      const label = x.fra === 0 ? "ny" : x.til === 0 ? "solgt" : `${pct(x.fra)} → ${pct(x.til)}`;
      return `<li><span>${esc(x.navn)}</span><span><span class="from">${label}</span><span class="d ${d > 0 ? "up" : "down"}">${signed(d)}</span></span></li>`;
    };
    const buys = c.filter((x) => x.til > x.fra), sells = c.filter((x) => x.til < x.fra).reverse();
    $("#buys").innerHTML = buys.map(item).join("") || `<li class="empty">Ingen kjøp.</li>`;
    $("#sells").innerHTML = sells.map(item).join("") || `<li class="empty">Ingen salg.</li>`;
  }

  // ───────────── utvikling ─────────────
  function renderPerf() {
    const M = DATA.metoder[method];
    const P = palette();
    const src = period === "live" ? M.live : M.backtest;
    const empty = $("#perfEmpty"), body = $("#perfBody");
    const S = DATA.innstillinger;
    if (period === "live") {
      $("#perfSub").textContent = M.live.start
        ? `Faktisk sporing fra ${dateLong(M.live.start)}. Vektene lagres i repoet ved hver rebalansering, og avkastningen regnes etter ${pct(S.transaksjonskostnad, 2)} i transaksjonskostnad.`
        : "";
    } else {
      $("#perfSub").textContent = `Simulert fra ${dateLong(S.backtest_start)} med samme regler som live: månedlig rebalansering, kun data som var tilgjengelig på hvert tidspunkt, og ${pct(S.transaksjonskostnad, 2)} i transaksjonskostnad. Kandidatlisten er dagens OSEBX-selskaper, så selskaper som har falt ut av børsen er ikke med. Det gjør backtesten for optimistisk.`;
    }
    if (!src.serie) {
      empty.hidden = false; body.hidden = true;
      empty.textContent = period === "live"
        ? `Live-sporingen startet ${dateLong(M.live.start)}. Første handel skjer til sluttkurs neste børsdag, og grafen vises fra dagen etter.`
        : "Backtesten har for lite data ennå.";
      return;
    }
    empty.hidden = true; body.hidden = false;

    const t = src.serie.datoer.map((d) => new Date(d + "T12:00:00").getTime());
    const span = (t[t.length - 1] - t[0]) / 864e5;
    const fmt = (v) => new Date(v).toLocaleDateString("nb-NO", span > 700 ? { year: "numeric" } : { month: "short", year: "2-digit" });
    const xy = (arr) => arr.map((v, i) => ({ x: t[i], y: v }));
    const o = baseOptions(P);
    o.interaction = { mode: "index", intersect: false };
    o.scales = {
      x: axis(P, { type: "linear", min: t[0], max: t[t.length - 1], ticks: { ...axis(P).ticks, callback: fmt, maxTicksLimit: 8 }, grid: { display: false } }),
      y: axis(P, { ticks: { ...axis(P).ticks, callback: (v) => nf(0).format(v) } }),
    };
    o.plugins.legend = { display: true, position: "top", align: "start", labels: { color: P.ink2, boxWidth: 18, boxHeight: 3, font: { family: css("--font") } } };
    o.plugins.tooltip.callbacks = {
      title: (it) => new Date(it[0].raw.x).toLocaleDateString("nb-NO", { day: "numeric", month: "short", year: "numeric" }),
      label: (it) => `${it.dataset.label}: ${nf(1).format(it.raw.y)}`,
    };
    mount("perfChart", { type: "line", data: { datasets: [
      { label: "Portefølje (start = 100)", data: xy(src.serie.portefolje), borderColor: P.fjord, borderWidth: 2.25, pointRadius: 0 },
      { label: DATA.indeks, data: xy(src.serie.indeks), borderColor: P.slate, borderWidth: 1.75, pointRadius: 0 },
    ] }, options: o });

    const dd = (arr) => { let m = -Infinity; return arr.map((v) => { m = Math.max(m, v); return (v / m - 1) * 100; }); };
    const o2 = baseOptions(P);
    o2.interaction = { mode: "index", intersect: false };
    o2.scales = {
      x: axis(P, { type: "linear", min: t[0], max: t[t.length - 1], ticks: { display: false }, grid: { display: false } }),
      y: axis(P, { max: 0, ticks: { ...axis(P).ticks, callback: (v) => v + " %", maxTicksLimit: 4 }, title: { display: true, text: "Fall fra topp", color: P.ink2 } }),
    };
    o2.plugins.tooltip.callbacks = { title: () => "", label: (it) => `${it.dataset.label}: ${nf(1).format(it.raw.y)} %` };
    mount("ddChart", { type: "line", data: { datasets: [
      { label: "Portefølje", data: xy(dd(src.serie.portefolje)), borderColor: P.red, backgroundColor: P.red + "22", fill: "origin", borderWidth: 1.5, pointRadius: 0 },
      { label: DATA.indeks, data: xy(dd(src.serie.indeks)), borderColor: P.slate, borderWidth: 1, pointRadius: 0 },
    ] }, options: o2 });

    const k = src.nokkeltall;
    const rows = [
      ["Total avkastning", "total", (x) => pct(x)],
      ["Årlig avkastning", "arlig", (x) => pct(x)],
      ["Volatilitet", "vol", (x) => pct(x)],
      ["Sharpe-ratio", "sharpe", (x) => num(x)],
      ["Største fall fra topp", "maks_fall", (x) => pct(x)],
      ["Beta mot " + DATA.indeks, "beta", (x) => num(x)],
      ["Tracking error", "tracking_error", (x) => pct(x)],
      ["Informasjonsrate", "informasjonsrate", (x) => num(x)],
    ];
    $("#metricTable tbody").innerHTML = k ? rows.map(([l, key, f]) =>
      `<tr><td>${l}</td><td class="num">${f(k.portefolje[key])}</td><td class="num">${f(k.indeks[key])}</td></tr>`).join("")
      : `<tr><td colspan="3">For kort periode til å beregne nøkkeltall.</td></tr>`;
  }

  // ───────────── år for år ─────────────
  function renderYears(M) {
    const P = palette();
    const Y = M.backtest.arlig;
    $("#yearSection").hidden = !Y.length;
    if (!Y.length) return;
    const o = baseOptions(P);
    o.plugins.legend = { display: true, position: "top", align: "start", labels: { color: P.ink2, boxWidth: 12, font: { family: css("--font") } } };
    o.scales = {
      x: axis(P, { grid: { display: false } }),
      y: axis(P, { ticks: { ...axis(P).ticks, callback: (v) => v + " %" } }),
    };
    o.plugins.tooltip.callbacks = { label: (it) => `${it.dataset.label}: ${nf(1).format(it.raw)} %` };
    mount("yearChart", { type: "bar", data: {
      labels: Y.map((y) => (y.hele_aret ? String(y.ar) : y.ar + "*")),
      datasets: [
        { label: "Portefølje", data: Y.map((y) => y.portefolje * 100), backgroundColor: P.fjord, borderRadius: 2 },
        { label: DATA.indeks, data: Y.map((y) => y.indeks * 100), backgroundColor: P.slate + "aa", borderRadius: 2 },
      ] }, options: o });
  }

  // ───────────── univers ─────────────
  function renderUniverse() {
    const U = DATA.univers, S = DATA.innstillinger;
    const list = (arr, f) => arr.length ? `<ul>${arr.map(f).join("")}</ul>` : "<p>Ingen.</p>";
    $("#uniBody").innerHTML = `
      <div><div class="big">${U.kandidater}</div><p>kandidater i kandidatlisten (basert på OSEBX)</p></div>
      <div><div class="big">${U.med_data}</div><p>har kursdata fra Yahoo</p></div>
      <div><div class="big">${U.godkjent}</div><p>er likvide nok og har ${Math.round(S.vindu_uker / 52)} års historikk, og kan velges i dag</p></div>
      <details><summary>For lite omsetning (${U.for_illikvide.length})</summary>
        <p>Krav: median dagsomsetning minst ${nf(0).format(S.min_omsetning_mnok)} MNOK de siste tre månedene.</p>
        ${list(U.for_illikvide, (x) => `<li>${esc(x.navn)}: ${x.omsetning_mnok == null ? "–" : nf(1).format(x.omsetning_mnok)} MNOK</li>`)}
      </details>
      <details><summary>Mangler data fra Yahoo (${U.mangler_data.length})</summary>
        <p>Tickeren kan være feil eller avnotert. Rett eller fjern den i kandidater.csv.</p>
        ${list(U.mangler_data, (x) => `<li>${esc(x.navn)} (${esc(x.ticker)})</li>`)}
      </details>`;
  }

  // ───────────── metode ─────────────
  function renderMethod(M) {
    const S = DATA.innstillinger, e = M.estimering;
    const common = `
      <p>Hver måned velger modellen den porteføljen som gir høyest forventet Sharpe-ratio, altså mest forventet avkastning over risikofri rente per enhet risiko. Det er tangentpunktet på den effisiente fronten til Harry Markowitz.</p>
      <h3>Hvilke aksjer kan velges</h3>
      <p>Utgangspunktet er alle aksjer i ${esc(DATA.indeks)}. En aksje er med bare hvis median dagsomsetning de siste tre månedene er minst ${nf(0).format(S.min_omsetning_mnok)} MNOK, den har handlet nesten hver dag, og den har minst ${Math.round(S.vindu_uker / 52)} års kurshistorikk.</p>
      <h3>Risiko</h3>
      <p>Kovariansen mellom aksjene beregnes fra ukentlige, utbyttejusterte avkastninger de siste ${S.vindu_uker} ukene. Den krympes mot en enklere struktur med Ledoit–Wolf-metoden${e.lw_shrinkage != null ? ` (krympingsgrad i dag: ${pct(e.lw_shrinkage, 0)})` : ""}, fordi rå historiske korrelasjoner gir ustabile vekter.</p>`;
    const bs = `
      <h3>Forventet avkastning: Bayes–Stein</h3>
      <p>Historiske snitt er svært usikre. Bayes–Stein-metoden (Jorion, 1986) trekker hver aksjes snittavkastning mot avkastningen til minimum-varians-porteføljen. Jo mer spredt og støyete snittene er, jo sterkere trekkes de sammen${e.krympingsgrad != null ? `. I dag trekkes de ${pct(e.krympingsgrad, 0)} av veien` : ""}.</p>`;
    const bl = `
      <h3>Forventet avkastning: Black–Litterman</h3>
      <p>Utgangspunktet er avkastningen markedet «krever» for å holde dagens markedsvekter (likevektsavkastning). Den justeres mot de historiske snittene, men bare så mye som usikkerheten i snittene tillater. Resultatet er mer stabile vekter som ligner mer på indeksen${e.tau != null ? ` (τ = ${num(e.tau)}, risikoaversjon ${num(e.risikoaversjon, 1)})` : ""}.</p>`;
    const rules = `
      <h3>Regler for porteføljen</h3>
      <ul>
        <li>Kun kjøp, ingen shortsalg.</li>
        <li>Maks ${pct(S.maks_vekt, 0)} i ett selskap og maks ${pct(S.maks_sektorvekt, 0)} i én sektor.</li>
        <li>Mellom ${S.min_antall} og ${S.maks_antall} selskaper, hver på minst ${pct(S.min_vekt, 0)}.</li>
        <li>Maks ${pct(S.maks_omsetning, 0)} av porteføljen byttes ut per rebalansering. Hvis det er umulig å oppfylle de andre kravene, lempes denne grensen, og det står i merknaden over.</li>
        <li>En posisjon skal kunne selges på én dag uten å utgjøre mer enn ${pct(S.deltakelsesgrad, 0)} av dagsomsetningen.</li>
      </ul>
      <p>Antallsgrensen gjør problemet vanskelig å løse eksakt. Modellen løser derfor først uten den, beholder de største posisjonene og løser på nytt til alle krav er oppfylt.</p>
      <p>Beslutningen tas etter børsslutt første handelsdag i måneden, og handelen regnes til sluttkurs dagen etter.</p>`;
    $("#methodProse").innerHTML = common + (method === "bayes_stein" ? bs : bl) + rules;
  }
})();
