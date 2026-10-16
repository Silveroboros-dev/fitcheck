"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

class Element {
  constructor() {
    this.hidden = false;
    this.disabled = false;
    this.textContent = "";
    this.innerHTML = "";
    this.value = "";
    this.checked = false;
    this.open = false;
    this.dataset = {};
    this.style = {};
    this.className = "";
    this.children = [];
    this.listeners = new Map();
    this.attributes = new Map();
    this.classList = { add: () => {}, remove: () => {}, toggle: () => {} };
  }
  addEventListener(name, callback) { this.listeners.set(name, callback); }
  append(...children) { this.children.push(...children); }
  setAttribute(name, value) { this.attributes.set(name, value); }
  removeAttribute(name) { this.attributes.delete(name); }
  focus() {}
}

const elements = new Map();
const $ = (id) => {
  if (!elements.has(id)) elements.set(id, new Element());
  return elements.get(id);
};
const document = {
  getElementById: $,
  createElement: () => new Element(),
  createTextNode: (text) => Object.assign(new Element(), { textContent: text }),
  querySelectorAll: () => [],
};
const storage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
const html = fs.readFileSync(process.argv[2], "utf8");
const script = html.slice(html.indexOf("<script>") + 8, html.lastIndexOf("</script>"));
const end = script.indexOf('$("choose-none").addEventListener');
assert.notEqual(end, -1, "market card helpers must precede market choice wiring");
const context = vm.createContext({
  document, localStorage: storage, sessionStorage: storage,
  fetch: () => Promise.reject(new Error("DOM harness must not make requests")),
  crypto: { randomUUID: () => "test-id" }, console,
});
vm.runInContext(script.slice(0, end), context);

const weakCard = {
  market_assessment_id: "assessment-weak",
  display_rank: 1,
  market_title: "Will Northstar Systems release Orion Suite by September 30, 2026?",
  pair_class: "weak_proxy",
  retrieval_rank: 1,
  market_id: "synthetic-release",
  what_it_captures: "This contract tests a launch for release of the next Orion Suite model for Northstar Systems, by September 30, 2026.",
  what_it_misses: "Your thesis tests a measured result for at least 42% share of the workshop-automation category for Northstar Systems, by December 31, 2027. A launch does not establish the at least 42% share of the workshop-automation category target.",
  resolution_conditions: "Synthetic resolution conditions.",
};

const directCard = {
  ...weakCard,
  market_assessment_id: "assessment-direct",
  pair_class: "direct",
  what_it_misses: "Your accepted thesis: Northstar Systems will not release Orion Suite by September 30, 2026. The recorded fit result maps this contract to the NO outcome for your thesis.",
};

function headingsFor(card) {
  context.card = card;
  const shell = vm.runInContext("assessmentCard(card)", context);
  const body = shell.children[1];
  const report = body.children[2];
  return {
    capture: report.children[0].children[0].textContent,
    comparison: report.children[1].children[0].textContent,
    captureText: report.children[0].children[1].textContent,
    comparisonText: report.children[1].children[1].textContent,
  };
}

const weak = headingsFor(weakCard);
assert.equal(weak.capture, "What this contract tests");
assert.equal(weak.comparison, "Why it does not cleanly test your thesis");
assert.match(weak.captureText, /Orion Suite model/);
assert.match(weak.comparisonText, /A launch does not establish/);
assert.doesNotMatch(weak.captureText + weak.comparisonText, /S1|M1|tok-v1|Loop 4/);

context.card = weakCard;
const weakShell = vm.runInContext("assessmentCard(card)", context);
const rules = weakShell.children[1].children[3];
assert.equal(rules.open, false, "resolution conditions start collapsed");
assert.equal(rules.children[0].textContent, "Contract resolution conditions");
assert.equal(rules.children[1].textContent, weakCard.resolution_conditions);

const direct = headingsFor(directCard);
assert.equal(direct.comparison, "Comparison with your thesis");
assert.doesNotMatch(direct.comparison, /does not cleanly/);
assert.match(direct.comparisonText, /will not release Orion Suite/);
assert.match(direct.comparisonText, /NO outcome/);
assert.doesNotMatch(direct.comparisonText, /align with your thesis/);

context.pool = {
  market_display_set_id: "display-set",
  snapshot_id: "snapshot",
  candidate_set_id: "candidate-set",
  system_pool_outcome: "no_clean_expression",
  displayed_count: 1,
  candidate_markets: [weakCard],
  accepted_thesis_summary: "Synthetic accepted thesis",
  retrieval_scope: null,
  assessed_count: 1,
  target_count: 1,
};
vm.runInContext("renderMarketPool(pool)", context);
assert.match($("pool-summary").textContent, /^No clean expression among the checked candidates\./);
assert.match($("pool-summary").textContent, /1 fully assessed displayed candidate/);
assert.match($("pool-summary").textContent, /not a conclusion about the whole market catalog/);


// The curated offline proposal and the ordinary diagnostic are separately
// attributed; complete frozen rules stay inspectable and no radio is selected.
const frozenCard = {
  ...weakCard, fit_confidence: null, authority: "offline_demo_proposal",
  frozen_demo_evidence: {
    schema_version: "fitcheck.frozen_demo_evidence.v3",
    policy_version: "frozen-demo-curation-v3", authority: "offline_demo_proposal",
    automatic_diagnostic: { pair_class: "weak_proxy", authority: "deterministic_only",
      fit_gate_policy_version: "loop3-m1-subject-only-v4" },
    yes_condition: "A qualifying public condition is met.",
    no_condition: "The condition is not met.",
    rule_deadline: "December 31, 2026, 23:59 ET (January 1, 2027, 04:59 UTC).",
    resolution_source: "Project-authored synthetic source.",
    fixture_versioned_utc: "2026-10-15T00:00:00Z",
    fixture_resource: "synthetic-market-broker-tokenized-asset.json",
    fixture_sha256: "synthetic-fixture-digest",
    rules_sha256: "synthetic-rule-digest",
    fixture_liquidity_usd: 1600,
    eligibility_policy_version: "frozen-demo-six-market-eligibility-v3",
    eligibility_liquidity_floor_usd: 0,
    market_url: "https://example.invalid/fitcheck/markets/synthetic-broker-tokenized-asset"
  }
};
context.frozenCard = frozenCard;
const frozenShell = vm.runInContext("assessmentCard(frozenCard)", context);
const frozenBody = frozenShell.children[1];
assert.equal(frozenShell.children[0].checked, false);
assert.match(frozenBody.children[1].textContent, /Frozen demo assessment/);
assert.doesNotMatch(frozenBody.children[1].textContent, /authority|policy|retrieval rank/);
assert.match(frozenBody.children[3].children[0].textContent, /^Yes:/);
assert.match(frozenBody.children[3].children[1].textContent, /^No:/);
assert.match(frozenBody.children[3].children[2].textContent, /04:59 UTC/);
assert.match(frozenBody.children[4].textContent, /Synthetic fixture versioned 2026-10-15/);
assert.match(frozenBody.children[4].textContent, /No current price or final outcome/);
const snapshotDetails = frozenBody.children[5];
assert.equal(snapshotDetails.open, false, "technical evidence starts collapsed");
assert.equal(snapshotDetails.children[0].textContent, "Snapshot and assessment details");
assert.match(snapshotDetails.children[1].textContent, /Curated assessment: weak proxy/);
assert.match(snapshotDetails.children[1].textContent, /Automatic diagnostic: weak proxy/);
assert.match(snapshotDetails.children[1].textContent, /frozen-demo-curation-v3/);
assert.match(snapshotDetails.children[2].textContent, /Project-authored synthetic fixture/);
assert.match(snapshotDetails.children[2].textContent, /Fixture liquidity: \$1600/);
assert.match(snapshotDetails.children[2].textContent, /Rules SHA-256 synthetic-rule-digest/);
assert.match(snapshotDetails.children[2].textContent, /project-authored fixture/);
assert.equal(snapshotDetails.children[3].href,
  "/assets/demo-fixtures/markets/synthetic-market-broker-tokenized-asset.json");
assert.notEqual(snapshotDetails.children[3].href, frozenCard.frozen_demo_evidence.market_url);
assert.doesNotMatch(snapshotDetails.children[3].href, /example\.invalid/);
assert.equal(snapshotDetails.children[3].textContent, "View packaged synthetic fixture");
assert.equal(frozenBody.children[6].open, false, "complete rules start collapsed");
assert.equal(frozenBody.children[6].children[1].textContent, frozenCard.resolution_conditions);

const v3Cards = [
  "synthetic-broker-tokenized-asset",
  "synthetic-broker-first-tokenized",
  "synthetic-broker-chain-token",
].map((mid, index) => ({
  ...frozenCard,
  market_id: mid, market_assessment_id: "assessment-" + mid,
  display_rank: index + 1, snapshot_id: "frozen-demo-three",
  candidate_set_member_id: "member-" + mid,
  rules_capture_id: "rules-" + mid,
  pair_class: index === 2 ? "not_an_expression" : "weak_proxy",
  frozen_demo_evidence: {
    ...frozenCard.frozen_demo_evidence,
    policy_version: "frozen-demo-curation-v3", market_id: mid,
    pair_class: index === 2 ? "not_an_expression" : "weak_proxy",
    snapshot_id: "frozen-demo-three", thesis_structure_sha256: "exact-thesis",
    fixture_versioned_utc: "2026-10-15T00:00:00Z",
    fixture_resource: "synthetic-market-" + mid.replace("synthetic-", "") + ".json",
    condition_id: "condition-" + mid, fixture_sha256: "fixture-" + mid,
    yes_no_summary_basis: "project_authored_market_rules",
    child_specific_yes_no_prose: true,
    event_association: index === 1
      ? { event_id: "synthetic-event-tokenized-listing", parent_description_differs: false }
      : null,
  },
}));
context.v3Cards = v3Cards;
const b2 = vm.runInContext("assessmentCard(v3Cards[1])", context);
assert.equal(b2.children[1].children[3].children.length, 4);
assert.match(b2.children[1].children[5].children[2].textContent, /Matched event synthetic-event-tokenized-listing/);
const b3 = vm.runInContext("assessmentCard(v3Cards[2])", context);
assert.match(b3.children[1].children[5].children[1].textContent, /Curated assessment: not an expression/);
context.v3Pool = {
  ...context.pool, thesis_analysis_id: "thesis", snapshot_id: "frozen-demo-three",
  snapshot_as_of: "2026-10-15T00:00:00Z",
  candidate_markets: v3Cards, displayed_count: 3, assessed_count: 3,
  target_count: 3, retrieval_scope: {query_binding: "exact-thesis", returned_count: 3,
    provider: "frozen-demo-six-market-v3", lifecycle_eligible_count: 3, gate_eligible_count: 3},
};
context.thesisId = "thesis";
assert.equal(vm.runInContext("validatePoolBinding(v3Pool, thesisId)", context), true);
context.v3Pool.candidate_markets = v3Cards.map((card, index) => index === 1
  ? {...card, frozen_demo_evidence: undefined}
  : card);
assert.equal(vm.runInContext("validatePoolBinding(v3Pool, thesisId)", context), false,
  "a mixed frozen-demo pool must fail closed when one card lacks v3 evidence");
context.v3Pool.candidate_markets = v3Cards.map((card) => {
  const {frozen_demo_evidence, ...withoutEvidence} = card;
  return withoutEvidence;
});
assert.equal(vm.runInContext("validatePoolBinding(v3Pool, thesisId)", context), false,
  "a frozen-demo snapshot must fail closed when every card lacks v3 evidence");
context.v3Pool.candidate_markets = v3Cards;
vm.runInContext("renderMarketPool(v3Pool)", context);
assert.match($("pool-summary").textContent, /3 fully assessed displayed candidates/);
assert.match($("pool-evidence").textContent, /project-authored fixture version/);
assert.doesNotMatch($("pool-evidence").textContent, /constituent capture/);
context.v3Pool.retrieval_scope = {...context.v3Pool.retrieval_scope,
  provider: "frozen-demo-six-market-v2"};
assert.equal(vm.runInContext("validatePoolBinding(v3Pool, thesisId)", context), false,
  "a frozen-demo pool must bind the available provider identity to v3");
context.v3Pool.retrieval_scope = {...context.v3Pool.retrieval_scope,
  provider: "frozen-demo-six-market-v3"};
delete context.v3Pool.retrieval_scope.provider;
assert.equal(vm.runInContext("validatePoolBinding(v3Pool, thesisId)", context), false,
  "a frozen-demo pool must require the v3 provider identity");
context.v3Pool.retrieval_scope = {...context.v3Pool.retrieval_scope,
  provider: "frozen-demo-six-market-v3"};
context.v3Pool.candidate_markets = [...v3Cards.slice(0, 2), {...v3Cards[2], market_id: "cross-bound"}];
assert.equal(vm.runInContext("validatePoolBinding(v3Pool, thesisId)", context), false);
context.v3Pool.candidate_markets = v3Cards.map((card, index) => index === 0
  ? {...card, frozen_demo_evidence: {...card.frozen_demo_evidence,
      fixture_resource: "synthetic-market-missing.json"}}
  : card);
assert.equal(vm.runInContext("validatePoolBinding(v3Pool, thesisId)", context), false,
  "a packaged fixture link must use an exact synthetic market filename");
context.v3Pool.candidate_markets = v3Cards.map((card, index) => index === 0
  ? {...card, frozen_demo_evidence: {...card.frozen_demo_evidence,
      policy_version: "frozen-demo-curation-v2"}}
  : card);
assert.equal(vm.runInContext("validatePoolBinding(v3Pool, thesisId)", context), false);
