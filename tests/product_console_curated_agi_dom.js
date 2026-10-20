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
const $ = id => {
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
assert.match(html, /src="\/assets\/brand\/fitcheck-logo-on-dark\.svg"/);
const script = html.slice(html.indexOf("<script>") + 8, html.lastIndexOf("</script>"));
vm.runInContext(script.slice(0, script.indexOf('api("/api/health").catch')), context);
context.metadata = {case_id:"agi2028", sha256:envelope.sha256};
vm.runInContext('anonymousDemo = true; demoSessionRef = "visitor"; curatedAgiMetadata = metadata;', context);
const run = source => vm.runInContext(source, context);
const response = () => ({...structuredClone(envelope), session_ref:"visitor"});
const allText = element => [element.textContent, ...element.children.flatMap(allText)].join(" ");

(async () => {
  $("s1").hidden = false;
  $("s2").hidden = false;
  $("s3").hidden = false;
  $("s4").hidden = false;
  $("s5").hidden = false;
  route = response;
  await run("loadCuratedAgiDemo()");
  assert.equal($("curated-agi-case").hidden, false);
  assert.equal($("historical-case").hidden, true);
  assert.equal($("s1").hidden, true);
  assert.equal($("s2").hidden, true);
  assert.equal($("s3").hidden, true);
  assert.equal($("s4").hidden, true);
  assert.equal($("s5").hidden, true);
  assert.equal(run("chain.thesisId"), null);
  assert.equal($("pool").hidden, true);
  assert.match($("curated-agi-thesis").textContent, /full cycle of AI research/);
  assert.match($("curated-agi-pool").textContent, /3 of 3/);
  assert.match($("curated-agi-pool").textContent, /No clean expression/);
  assert.equal($("curated-agi-cards").children.length, 3);
  assert.deepEqual($("curated-agi-cards").children.map(card => card.dataset.marketKey),
    ["rsi", "millennium", "agi"]);
  assert.match(allText($("curated-agi-cards").children[0]),
    /unreviewed relatedness proposal/);
  assert.match(allText($("curated-agi-cards").children[1]),
    /unchanged automatic label/);
  assert.match(allText($("curated-agi-cards").children[0]),
    /Project-authored condition summary/);
  assert.doesNotMatch(allText($("curated-agi-case")), /Human clarification|Original saved run/);
  assert.equal(calls.every(call => !call.opts.method || call.opts.method === "GET"), true);

  await $("curated-agi-earlier").onclick();
  assert.equal(window.location.search, "?demo_history=agi2028&history_version=original");
  assert.match($("curated-agi-version-note").textContent, /Curated reconstruction/i);
  assert.match(allText($("curated-agi-cards").children[0]), /not an expression/i);
  assert.doesNotMatch(allText($("curated-agi-cards").children[0]), /unreviewed relatedness proposal/);
  assert.equal($("curated-agi-cards").children.length, 3);

  await $("curated-agi-current").onclick();
  assert.equal(window.location.search, "?demo_history=agi2028");
  assert.match(allText($("curated-agi-cards").children[0]), /unreviewed relatedness proposal/);

  window.location.search = "?demo_history=agi2028&history_version=original";
  run("resetDownstream()");
  await run("loadCuratedAgiDemo()");
  assert.equal($("curated-agi-case").hidden, false);
  assert.match(allText($("curated-agi-cards").children[0]), /not an expression/i);

  for (const change of [
    out => {out.case.markets[0].key = "wrong";},
    out => {out.session_ref = "other";},
    out => {out.sha256 = "other";},
    out => {out.case.accepted_state = {acceptance_origin:"human_ui"};}
  ]) {
    run("resetDownstream()");
    route = () => { const out = response(); change(out); return out; };
    await run("loadCuratedAgiDemo()");
    assert.equal($("curated-agi-case").hidden, true);
    assert.equal($("curated-agi-cards").children.length, 0);
    assert.match($("demo-journey-state").textContent, /unavailable/);
  }

  run("resetDownstream()");
  let release;
  route = () => new Promise(resolve => {release = resolve;});
  const pending = run("loadCuratedAgiDemo()");
  await new Promise(resolve => setImmediate(resolve));
  run("resetDownstream()");
  release(response());
  await pending;
  assert.equal($("curated-agi-case").hidden, true);
  assert.equal(run("chain.thesisId"), null);

  window.location.search = "?demo_history=agi2028&resume_thesis_id=old";
  route = path => path === "/api/demo/reset"
    ? {session_ref:"visitor"} : response();
  await run('beginDemoJourney("history")');
  assert.equal($("curated-agi-case").hidden, false);
  assert.equal(window.location.search, "?demo_history=agi2028");
  assert.equal(calls.at(-1).path, "/api/demo/agi2028");

  await run('beginDemoJourney("source")');
  assert.equal($("curated-agi-case").hidden, true);
  assert.equal($("s1").hidden, false);
  assert.equal($("s3").hidden, false);
  assert.equal(window.location.search, "");
  assert.equal($("run").disabled, false);
  assert.match(html, /id="demo-source"/);
  assert.match(html, /id="demo-hormuz"/);
  assert.match(html, /id="demo-history"/);

  run('var privateCalls = 0');
  run('historicalCaseMetadata = {case_id:"agi2028"}; loadHistoricalDemo = async () => { privateCalls += 1; };');
  await run('beginDemoJourney("history")');
  assert.equal(run("privateCalls"), 1);
  assert.equal(calls.at(-1).path, "/api/demo/reset");
  console.log("curated AGI DOM: read-only, version switch, stale response, reset, private precedence passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
