"use strict";
// Renders Touchstone's published results from data/*.json.
// Every number, verdict and address comes from those files.

(function () {
  var REPO = "https://github.com/mishoko/touchstone/blob/main/";

  // ---------- small helpers ----------
  function el(tag, attrs, kids) {
    var n = document.createElement(tag);
    if (attrs) Object.keys(attrs).forEach(function (k) {
      if (k === "text") n.textContent = attrs[k];
      else if (k === "cls") n.className = attrs[k];
      else n.setAttribute(k, attrs[k]);
    });
    (kids || []).forEach(function (c) {
      if (c == null) return;
      n.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    });
    return n;
  }
  function num(x, d) {
    return Number(x).toLocaleString("en-US", { maximumFractionDigits: d == null ? 2 : d });
  }
  function vals(o) { return Object.keys(o).map(function (k) { return o[k]; }); }
  function getJSON(path) {
    return fetch(path).then(function (r) {
      if (!r.ok) throw new Error(path + ": HTTP " + r.status);
      return r.text();
    }).then(function (t) {
      // Python's json writes Infinity and NaN; read them as strings.
      return JSON.parse(t.replace(/([:\[,]\s*)(-?Infinity|NaN)(?=\s*[,\]}])/g, '$1"$2"'));
    });
  }
  function badgeEl(text, verdict) {
    var cls = "badge badge-" + String(verdict || "none").toLowerCase().replace(/_/g, "-");
    return el("span", { cls: cls, text: text || "no verdict" });
  }
  function sourceLinks(row) {
    return el("span", { cls: "src" }, [
      el("a", { href: REPO + "eval/results/" + row.file, text: "eval/results/" + row.file }),
      " ",
      el("a", { href: "data/" + row.file, cls: "local", text: "(local copy)" })
    ]);
  }
  function cmd(test) {
    var text = "forge test --mt " + test;
    var b = el("button", { type: "button", cls: "copy", "aria-label": "Copy command: " + text, text: "Copy" });
    b.addEventListener("click", function () {
      if (navigator.clipboard) navigator.clipboard.writeText(text).then(function () {
        b.textContent = "Copied";
        setTimeout(function () { b.textContent = "Copy"; }, 1500);
      });
    });
    return el("div", { cls: "cmd" }, [el("code", { text: text }), b]);
  }
  function tests(names, summary) {
    if (!names || !names.length) return null;
    var list = names.map(cmd);
    if (names.length <= 3) return el("div", { cls: "tests" }, list);
    return el("details", { cls: "tests" }, [el("summary", { text: summary || "Tests" })].concat(list));
  }
  function p(text) { return el("p", { text: text }); }

  // ---------- per-result copy and key numbers ----------
  // Questions are short plain-language versions of each file's question.
  var Q = {
    "E1": "Is the witness a real position?",
    "E2": "Does the charge cover an honest move's loss?",
    "E3": "Is the charge zero when the curator does nothing?",
    "E4": "Does the reference curator beat idle, dust, stale and wide ones?",
    "E5": "Do the asserted attacks lose money?",
    "E6": "Does churning pay?",
    "E7": "Are gas and contract size within budget?",
    "E8": "Operating numbers.",
    "E9": "Why is positioning not paid?",
    "U-IC2": "Incentives: does the reference curator out-earn lazy and gaming bots?",
    "E-IC": "Incentives on a historical price path.",
    "E0b": "Does the allocation ledger close within its bound?",
    "U-IC": "Incentives, earlier six-hour form.",
    "E0": "Ledger closure, registered form.",
    "E0a": "Ledger closure, amended form.",
    "E0b-fit": "Ledger fit data used to set up E0b."
  };

  function e0bStats(d) {
    var rows = [];
    vals(d.runs).forEach(function (r) { rows = rows.concat(r.rows); });
    var above = rows.filter(function (r) { return Math.abs(r.residual_bp) > d.bound_bp; }).length;
    var worst = Math.max.apply(null, vals(d.runs).map(function (r) { return r.worst_bp; }));
    return { above: above, total: rows.length, worst: worst, bound: d.bound_bp };
  }

  var KEY = {
    "E1": function (d) {
      var n = d.numbers;
      return [p(num(n.max_difference_wei) + " wei difference."),
              p("Exact equality on " + num(n.fuzz_runs) + " fuzz runs."),
              tests(n.related_tests_passed)];
    },
    "E2": function (d) {
      var a = d.numbers.sigma_60, b = d.numbers.sigma_120;
      return [p("Overcharge " + num(a.overcharge_mean_bp, 3) + " / " + num(b.overcharge_mean_bp, 3) +
                " bp of NAV per placement, mean."),
              p("At σ 60% / 120%."),
              p("Shortfall rows: " + num(a.short_fee_aware) + " of " + num(a.placements) + " / " +
                num(b.short_fee_aware) + " of " + num(b.placements) + ".")];
    },
    "E3": function (d) {
      var n = d.numbers;
      return [p(num(n.charge_wei) + " wei charged over " + num(n.segments) + " segments."),
              p(num(n.third_party_swaps) + " third-party swaps."),
              p("Control: one curator swap is charged."),
              tests(d.evidence && d.evidence.tests)];
    },
    "E4": function (d) {
      var bs = vals(d.buckets);
      var means = bs.map(function (b) { return b.mimic_net_mean; });
      var bots = {}, ok = 0, all = 0;
      bs.forEach(function (b) { vals(b.bots).forEach(function (x) { all++; if (x.ok) ok++; }); Object.keys(b.bots).forEach(function (k) { bots[k] = 1; }); });
      return [p("Reference curator nets " + num(Math.min.apply(null, means)) + " to " +
                num(Math.max.apply(null, means)) + " ETH per path, mean."),
              p("It beats " + Object.keys(bots).join(", ") + " in " + ok + " of " + all + " comparisons."),
              tests(d.identity_test && [d.identity_test.name])];
    },
    "E5": function (d) {
      var cells = vals(d.cells);
      var ok = cells.filter(function (c) { return c.ok; }).length;
      var controls = [];
      cells.forEach(function (c) { (c.controls || []).forEach(function (t) { controls.push(t.test); }); });
      var withCtl = cells.filter(function (c) { return (c.controls || []).length > 0; }).length;
      return [p("Every asserted attack row nets ≤ 0 in " + ok + " of " + cells.length + " cells."),
              p(withCtl + " cells have a control test that profits with the defence removed."),
              el("p", { cls: "fail-text" }, ["Why FAIL: two cells have only inline sensitivity checks. " +
                "The registered rule exempted one forgery cell alone."]),
              p("Under the rule as amended after the data: " + d.amended_rule_verdict + "."),
              tests(controls, controls.length + " control tests")];
    },
    "E6": function (d) {
      var t = vals(d.buckets).map(function (b) { return b.t; });
      return [p("Churning loses to the reference curator in every bucket."),
              p("t-statistic " + num(Math.max.apply(null, t), 0) + " to " + num(Math.min.apply(null, t), 0) + "."),
              p("This follows from construction.")];
    },
    "E7": function (d) {
      var s = d.sizes_runtime_bytes, name = Object.keys(s).sort(function (a, b) { return s[b] - s[a]; })[0];
      var g = vals(d.gas), within = g.filter(function (x) { return x.ok; }).length;
      return [p("Largest runtime " + num(s[name]) + " B (" + name + ")."),
              p("Limit " + num(d.size_limit_bytes) + " B."),
              p(within + " of " + g.length + " gas rows within budget.")];
    },
    "E8": function (d) {
      var items = Object.keys(d.items).length;
      return [p(items + " items. " + d.not_run_count + " of them not run."),
              p("Charges and push costs are in the tables below.")];
    },
    "E9": function (d) {
      var r = d.numbers.random_curator_pay_above_base;
      var m = r["Mandate benchmark"], x = r["Exposure-matched"];
      var regimes = Object.keys(m);
      return [p("A random curator's pay above base, by regime (" + regimes.join(" / ") + "):"),
              p("Carry benchmark: " + regimes.map(function (k) { return m[k].pay_above_base_pct.toFixed(1) + "%"; }).join(" / ") + "."),
              p("Exposure-matched: " + regimes.map(function (k) { return x[k].pay_above_base_pct.toFixed(1) + "%"; }).join(" / ") + "."),
              p("Design-phase simulator. Not re-run.")];
    },
    "U-IC2": function (d) {
      var bs = vals(d.buckets), bots = d.gated_bots.length;
      var allOk = bs.every(function (b) { return vals(b.cmp).every(function (c) { return c.ok; }); });
      return [p((allOk ? "Beats all " : "Does not beat all ") + bots + " gated bots in " + bs.length + " buckets."),
              p("Held-out seeds " + d.seeds[0] + " to " + d.seeds[1] + "."),
              p("Synthetic paths. No outside observer.")];
    },
    "E-IC": function (d) {
      return [p("Not run."),
              p("No historical price path was fetched."),
              p((d.synthetic_evidence ? d.synthetic_evidence.id : "U-IC2") + " covers its comparison on synthetic paths only.")];
    },
    "E0b": function (d) {
      var s = e0bStats(d);
      return [p(s.above + " of " + s.total + " epoch rows above " + s.bound + " bp."),
              p("Worst row " + num(s.worst) + " bp."),
              p("The closure claim is withdrawn.")];
    },
    "U-IC": function (d) {
      return [p("Earlier 6-hour reference policy."), p("Two comparisons failed at 120% volatility."),
              p("Published as failed. Never re-run.")];
    },
    "E0": function (d) { return [p("Registered form failed."), p("Superseded by " + d.superseded_by + ".")]; },
    "E0a": function (d) { return [p("Amended form failed."), p("Published as a negative result.")]; },
    "E0b-fit": function (d) { return [p("Fit data, no verdict.")]; }
  };

  function renderRow(row, d) {
    var key = KEY[row.id] ? KEY[row.id](d) : [p(String(d.question || ""))];
    var tr = el("tr", { id: "r-" + row.id, cls: "row-" + String(row.verdict || "none").toLowerCase() }, [
      el("th", { scope: "row", "data-label": "ID" }, [el("a", { href: REPO + "eval/results/" + row.file, text: row.id })]),
      el("td", { "data-label": "Question", text: Q[row.id] || String(d.question || row.id) }),
      el("td", { "data-label": "Verdict" }, [badgeEl(row.badge, row.verdict)]),
      el("td", { "data-label": "Key number", cls: "key" }, key),
      el("td", { "data-label": "Source" }, [sourceLinks(row)])
    ]);
    return tr;
  }

  // ---------- E8 figures ----------
  function e8Charges(d) {
    var it = d.items.reference_charge_distribution;
    var fig = document.getElementById("e8-charges");
    var rows = Object.keys(it.value).map(function (k) {
      return el("tr", null, [el("th", { scope: "row", text: k }),
        el("td", { cls: "n", text: num(it.value[k].charges_bps_of_nav_per_year) }),
        el("td", { cls: "n", text: num(it.value[k].actions_per_path, 1) })]);
    });
    fig.appendChild(el("figcaption", null, [el("strong", { text: "Rebalancing cost charged to the reference curator." }),
      " Basis points of NAV per year. Synthetic paths. Source: ",
      el("a", { href: REPO + "eval/results/E8.json", text: "E8" }), " items.reference_charge_distribution."]));
    fig.appendChild(el("table", { cls: "small" }, [
      el("thead", null, [el("tr", null, [el("th", { scope: "col", text: "Bucket" }), el("th", { scope: "col", cls: "n", text: "bps / yr" }), el("th", { scope: "col", cls: "n", text: "Actions per path" })])]),
      el("tbody", null, rows)]));
  }
  function kref(d) {
    var it = d.items.forfeiture_manipulation_cost_kref, v = it.value;
    var fig = document.getElementById("kref");
    var rows = v.per_block_measured.map(function (r) {
      var hold = v.hold_24h_competitive_bound_weth[String(r.push_ticks)];
      return el("tr", null, [el("th", { scope: "row", text: r.push_ticks + " ticks" }),
        el("td", { cls: "n", text: num(r.one_block_hold_weth, 4) }),
        el("td", { cls: "n", text: num(r.round_trip_weth, 4) }),
        el("td", { cls: "n", text: hold == null ? "—" : num(hold, 1) })]);
    });
    fig.appendChild(el("figcaption", null, [el("strong", { text: "Cost of moving the reference TWAP (K-ref)." }),
      " WETH. Measured per block on a mainnet fork. The 24 h column is arithmetic. It assumes arbitrage every block. Source: ",
      el("a", { href: REPO + "eval/results/E8.json", text: "E8" }), " items.forfeiture_manipulation_cost_kref."]));
    fig.appendChild(el("table", { cls: "small" }, [
      el("thead", null, [el("tr", null, ["Push", "Hold one block", "Round trip", "Hold 24 h"].map(function (h, i) {
        return el("th", { scope: "col", cls: i ? "n" : "", text: h }); }))]),
      el("tbody", null, rows)]));
    fig.appendChild(el("p", { cls: "note", text: "One builder that wins every block pays about one round trip. That case is outside the model." }));
  }

  // ---------- limits ----------
  function limits(docs) {
    var ul = document.getElementById("limits-list");
    ul.textContent = "";
    function item(title, lines, id) {
      ul.appendChild(el("li", null, [el("strong", { text: title }), " "].concat(
        lines.map(function (l) { return typeof l === "string" ? l + " " : l; }),
        id ? [el("a", { href: REPO + "eval/results/" + id + ".json", cls: "cite", text: "[" + id + "]" })] : [])));
    }
    var ll = docs.E8.items.lag_leak_per_sigma.value;
    item("Lag leak.", ["Prices an action reads lag the market by a block.",
      "A delayed settlement closes the gap in expectation, not on every draw.",
      "Measured leak: " + ll.sigma_60_pct_nav_per_year.toFixed(3) + "% / " + ll.sigma_120_pct_nav_per_year.toFixed(3) +
        "% of NAV a year at σ 60% / 120%, simulated, from an earlier configuration."], "E8");
    var tw = {};
    vals(docs.E5.cells).forEach(function (c) { var t = c.reported_unasserted_tails_wei || {}; Object.keys(t).forEach(function (k) { tw[k] = t[k]; }); });
    function u(k) { return tw[k] == null ? "—" : "+" + num(tw[k] / 1e18, 5); }
    item("Reported, not asserted.", ["Some attack rows net a positive amount. No test asserts them.",
      "A markout past the settlement cap: " + u("perDrawUncappedOnLegLoses") + " (about 8e-4 of NAV).",
      "A protocol fee switched on: " + u("protocolFeeStopsReported") + ".",
      "Arbitrage stopping off the legs: up to " + u("bothWorldsOvershootReported") + ".",
      "Parking while the cached reference lags: " + u("parkingWhileTheCacheLagsIsBoundedByItsInRangeTime") +
        " per exit on the test tier; at most about 5e-6 of NAV per exit on mainnet.",
      "Units: tokens in a test vault of about 1,150 NAV."], "E5");
    item("E-IC was not run.", ["No historical price path was fetched.",
      "The incentives result " + docs["E-IC"].synthetic_evidence.id + " is synthetic.",
      "It has no outside observer."], "E-IC");
    var s = e0bStats(docs.E0b);
    item("Allocation ledger claim withdrawn.", ["E0b failed.",
      s.above + " of " + s.total + " epoch rows exceeded " + s.bound + " bp.",
      "The ledger is disclosure only."], "E0b");
    item("Synthetic paths only.", ["Every simulated result uses simulated 180-day paths.",
      "No historical price data is used."]);
  }

  // ---------- testnet ----------
  function testnet(dep, file) {
    var dl = document.getElementById("addresses");
    function add(label, value) {
      var isAddr = /^0x[0-9a-fA-F]{40}$/.test(value), isTx = /^0x[0-9a-fA-F]{64}$/.test(value);
      if (!isAddr && !isTx) return;
      var href = "https://sepolia.uniscan.xyz/" + (isAddr ? "address/" : "tx/") + value;
      dl.appendChild(el("div", null, [el("dt", { text: label.replace(/_/g, " ") }),
        el("dd", null, [el("a", { href: href, cls: "mono", text: value })])]));
    }
    [dep.deploy, dep.step0b].forEach(function (grp) {
      Object.keys(grp).forEach(function (k) { add(k, String(grp[k])); });
    });
    var src = document.getElementById("deploy-src");
    src.appendChild(document.createTextNode("Deployed " + dep.date + " from " + dep.code + ". Source: "));
    src.appendChild(el("a", { href: REPO + "script/demo/" + file, text: "script/demo/" + file }));
  }

  // ---------- stepper ----------
  function stepper() {
    var steps = Array.prototype.slice.call(document.querySelectorAll("#steps > li"));
    var track = Array.prototype.slice.call(document.querySelectorAll("#stepper .track > li"));
    var nav = document.querySelector(".step-nav"), pos = document.getElementById("step-pos");
    var prev = document.getElementById("prev"), next = document.getElementById("next");
    var i = 0;
    document.getElementById("stepper").classList.add("js");
    nav.hidden = false;
    function show() {
      steps.forEach(function (s, j) { s.hidden = j !== i; });
      track.forEach(function (t, j) { t.classList.toggle("on", j === i); t.classList.toggle("done", j < i); });
      prev.disabled = i === 0; next.disabled = i === steps.length - 1;
      pos.textContent = "Step " + (i + 1) + " of " + steps.length;
    }
    prev.addEventListener("click", function () { if (i > 0) { i--; show(); } });
    next.addEventListener("click", function () { if (i < steps.length - 1) { i++; show(); } });
    show();
  }

  // ---------- main ----------
  function fatal(err) {
    var ul = document.getElementById("glance");
    ul.textContent = "";
    ul.appendChild(el("li", { cls: "fail-text", text: "Could not load the result files: " + err.message }));
  }

  stepper();
  getJSON("data/manifest.json").then(function (man) {
    if (man.repo) REPO = man.repo;
    return Promise.all(man.results.map(function (r) { return getJSON("data/" + r.file); }).concat(
      [getJSON("data/" + man.deployments)])).then(function (all) {
      var docs = {};
      man.results.forEach(function (r, j) { docs[r.id] = all[j]; });
      var glance = document.getElementById("glance"), body = document.getElementById("results-body"),
          hist = document.getElementById("history-body");
      glance.textContent = "";
      man.results.forEach(function (r) {
        if (r.section === "main") {
          body.appendChild(renderRow(r, docs[r.id]));
          glance.appendChild(el("li", null, [el("a", { href: "#r-" + r.id, text: r.id }), " ", badgeEl(r.badge, r.verdict)]));
          if (r.id === "E5") glance.appendChild(el("li", { cls: "note" }, ["Every asserted attack loses. Two controls are inline."]));
        } else {
          hist.appendChild(renderRow(r, docs[r.id]));
        }
      });
      e8Charges(docs.E8);
      kref(docs.E8);
      limits(docs);
      testnet(all[all.length - 1], man.deployments);
    });
  }).catch(fatal);
})();
