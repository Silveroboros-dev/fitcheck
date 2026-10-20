"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
class Element {
  constructor() {
    this.hidden = true; this.disabled = false; this.textContent = "";
    this._innerHTML = ""; this.value = ""; this.checked = false;
    this.dataset = {}; this.style = {}; this.children = [];
    this.classList = { add() {}, remove() {}, toggle() {} };
  }
  set innerHTML(value) { this._innerHTML = value; this.children = []; }
  get innerHTML() { return this._innerHTML; }
  addEventListener() {}
  setAttribute() {}
  removeAttribute() {}
  append(...children) { this.children.push(...children); }
  scrollIntoView() {}
  focus() {}
}
const elements = new Map();
const $ = (id) => {
  if (!elements.has(id)) elements.set(id, new Element());
  return elements.get(id);
};
const document = { getElementById: $, createElement: () => new Element(),
  createTextNode: () => new Element(), querySelectorAll: () => [] };
const storage = { getItem: () => null, setItem() {}, removeItem() {} };
const window = { location: { pathname: "/", search: "", hash: "" },
  history: { replaceState(_state, _title, url) {
    const parsed = new URL(url, "http://localhost");
    Object.assign(window.location, { search: parsed.search, hash: parsed.hash });
  } } };
const envelope = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const calls = [];
let route;
const context = vm.createContext({
  document, window, localStorage: storage, sessionStorage: storage,
  URLSearchParams, crypto: { randomUUID: () => "request" }, console,
  fetch: async (path, opts) => {
    calls.push({path, opts});
    return {ok:true, json:async () => route(path)};
  }
});
const html = fs.readFileSync(process.argv[2], "utf8");
const script = html.slice(html.indexOf("<script>") + 8, html.lastIndexOf("</script>"));
vm.runInContext(script.slice(0, script.indexOf('api("/api/health").catch')), context);
context.metadata = {case_id:"agi2028", sha256:envelope.sha256};
vm.runInContext('anonymousDemo = true; demoSessionRef = "visitor"; historicalCaseMetadata = metadata;', context);
const run = (source) => vm.runInContext(source, context);
const response = () => ({...structuredClone(envelope), session_ref:"visitor"});
const isV2 = envelope.case.schema_version === "private-historical-demo-v2";
const allText = element => [element.textContent, ...element.children.flatMap(allText)].join(" ");

(async () => {
  route = response;
  await run("loadHistoricalDemo()");
  assert.equal($("historical-case").hidden, false);
  assert.equal($("norm-summary").textContent, envelope.case.accepted_state.accepted_thesis_wording);
  assert.equal($("pool-choice-controls").hidden, true);
  assert.equal($("source-entry-controls").hidden, true);
  assert.equal($("find-markets").disabled, true);
  assert.equal(run("chain.marketInspection"), true);
  assert.equal(run("chain.marketChoiceId"), null);
  const before = calls.length;
  await $("find-markets").onclick();
  assert.equal(calls.length, before);
  assert.equal(calls.every(call => !call.opts.method || call.opts.method === "GET"), true);
  assert.equal(run("JSON.parse(document.getElementById('historical-case').dataset.binding).sha256"), envelope.sha256);
  if (isV2) {
    assert.equal($("historical-version-controls").hidden, false);
    assert.equal($("historical-input-label").textContent,
      "Original thesis input (English translation)");
    assert.equal($("historical-acceptance-label").textContent,
      "Human clarification (English translation)");
    assert.equal($("historical-input").textContent,
      envelope.case.presentation.original_input_en);
    assert.equal($("historical-acceptance").textContent,
      envelope.case.presentation.acceptance_note_en);
    assert.doesNotMatch($("historical-input").textContent +
      $("historical-acceptance").textContent, /[А-Яа-яЁё]/);
    assert.equal(run("chain.marketCards[0].pair_class"), "weak_proxy");
    assert.match(allText($("market-grid")), /Source-bound relation proposal/);
    assert.match(allText($("market-grid")), /predecessor automatic: not an expression/);
    assert.match(allText($("market-grid")), /Frozen rule excerpt/);
    assert.equal($("pool-choice-controls").hidden, true);

    route = response;
    await $("historical-original").onclick();
    assert.equal(window.location.search,
      "?demo_history=agi2028&history_version=original");
    assert.equal(run("chain.marketCards[0].pair_class"), "not_an_expression");
    assert.equal(run("JSON.parse(document.getElementById('historical-case').dataset.binding).history_version"),
      "original");
    assert.doesNotMatch(allText($("market-grid")), /Source-bound relation proposal/);
    assert.equal($("historical-input").textContent,
      envelope.case.presentation.original_input_en);
    assert.equal($("pool-choice-controls").hidden, true);

    await $("historical-current").onclick();
    assert.equal(window.location.search, "?demo_history=agi2028");
    assert.equal(run("chain.marketCards[0].pair_class"), "weak_proxy");
    assert.equal($("pool-choice-controls").hidden, true);

    window.location.search = "?demo_history=agi2028&history_version=original";
    run("resetDownstream()");
    await run("loadHistoricalDemo()");
    assert.equal(run("chain.marketCards[0].pair_class"), "not_an_expression");
    assert.equal($("historical-input").textContent,
      envelope.case.presentation.original_input_en);

    let releaseOld;
    let requestCount = 0;
    route = () => ++requestCount === 1
      ? new Promise(resolve => { releaseOld = resolve; }) : response();
    const staleCurrent = run('loadHistoricalDemo()');
    await new Promise(resolve => setImmediate(resolve));
    await $("historical-current").onclick();
    releaseOld(response());
    await staleCurrent;
    assert.equal(run("chain.marketCards[0].pair_class"), "weak_proxy");
    assert.equal(run("JSON.parse(document.getElementById('historical-case').dataset.binding).history_version"),
      "current");
  }

  for (const change of [
    out => {out.case.accepted_state.latest_market_display_set_id = "other";},
    out => {out.case.market_pool.accepted_thesis_wording = "other";},
    out => {out.session_ref = "other";},
    out => {out.sha256 = "other";}
  ]) {
    run("resetDownstream()");
    route = () => { const out = response(); change(out); return out; };
    await run("loadHistoricalDemo()");
    assert.equal($("historical-case").hidden, true);
    assert.equal($("pool").hidden, true);
    assert.match($("demo-journey-state").textContent, /unavailable/);
  }

  run("resetDownstream()");
  let release;
  route = () => new Promise(resolve => {release = resolve;});
  const pending = run("loadHistoricalDemo()");
  await new Promise(resolve => setImmediate(resolve));
  run("resetDownstream()");
  release(response());
  await pending;
  assert.equal($("historical-case").hidden, true);
  assert.equal(run("chain.thesisId"), null);

  window.location.search = "?demo_history=agi2028&resume_thesis_id=old";
  let resetCount = 0;
  route = path => {
    assert.equal(path, "/api/demo/reset");
    resetCount++;
    return {session_ref:"new-visitor"};
  };
  await Promise.all([run('beginDemoJourney("source")'), run('beginDemoJourney("history")')]);
  assert.equal(resetCount, 1);
  assert.equal(window.location.search, "");
  assert.equal($("historical-case").hidden, true);
  assert.equal($("source-entry-controls").hidden, false);
  assert.equal($("run").disabled, false);
  assert.equal(run("historicalInspection"), false);
  console.log("historical demo DOM: read-only, versions, binding, stale response and journey reset passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
