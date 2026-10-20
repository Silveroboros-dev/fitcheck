"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

class Element {
  constructor() {
    this.hidden = false;
    this.disabled = false;
    this._textContent = "";
    this.value = "";
    this.checked = false;
    this.open = false;
    this.scrollCount = 0;
    this.focusCount = 0;
    this.dataset = {};
    this.style = {};
    this.children = [];
    this.listeners = new Map();
    this.classList = { add() {}, remove() {}, toggle() {} };
  }
  get textContent() {
    return this._textContent + this.children.map((child) => child.textContent).join("");
  }
  set textContent(value) { this._textContent = String(value); this.children = []; }
  get innerHTML() { return this._html || ""; }
  set innerHTML(value) { this._html = value; this._textContent = ""; this.children = []; }
  append(...items) { this.children.push(...items); }
  addEventListener(event, handler) { this.listeners.set(event, handler); }
  setAttribute() {}
  removeAttribute() {}
  focus() { this.focusCount += 1; }
  scrollIntoView() { this.scrollCount += 1; }
}

const elements = new Map();
const $ = (id) => {
  if (!elements.has(id)) elements.set(id, new Element());
  return elements.get(id);
};
const decisionRadios = [new Element(), new Element()];
decisionRadios[0].value = "dismiss";
decisionRadios[1].value = "reaffirm";
const marketRadios = [new Element(), new Element()];
const document = {
  getElementById: $,
  createElement: (tagName) => {
    const element = new Element();
    element.tagName = tagName.toUpperCase();
    return element;
  },
  createTextNode: (value) => ({ textContent: value }),
  querySelectorAll: (selector) => selector.includes("revisit-decision")
    ? decisionRadios : selector.includes("market-choice") ? marketRadios : [],
};
const storage = { getItem: () => null, setItem() {}, removeItem() {} };
const window = { location: { search: "", pathname: "/", hash: "" } };
window.history = {
  state: null,
  replaceState(_state, _title, path) {
    window.location.search = path.includes("?")
      ? "?" + path.split("?", 2)[1].split("#", 1)[0] : "";
  },
};
const calls = [];
let respond = () => { throw new Error("unexpected fetch"); };
const fetch = (path, options) => {
  calls.push({ path, options });
  return respond(path, options);
};
const html = fs.readFileSync(process.argv[2], "utf8");
const choiceStart = html.indexOf('<fieldset id="revisit-choice-set"');
const choiceEnd = html.indexOf('</fieldset>', choiceStart);
assert.ok(choiceStart > 0 && choiceEnd > choiceStart);
const choiceMarkup = html.slice(choiceStart, choiceEnd);
assert.match(choiceMarkup, /What would you like to do\?/);
assert.match(choiceMarkup, /Dismiss this review — no reaffirmation/);
assert.match(choiceMarkup, /Reviewed — keep the thesis unchanged/);
assert.match(choiceMarkup, /id="open-revisit-draft"[^>]*>Revise thesis…/);
assert.ok(choiceMarkup.indexOf('value="dismiss"') <
  choiceMarkup.indexOf('value="reaffirm"'));
assert.ok(choiceMarkup.indexOf('value="reaffirm"') <
  choiceMarkup.indexOf('id="open-revisit-draft"'));
assert.ok(html.indexOf('id="submit-revisit-decision"', choiceEnd) > choiceEnd);
const script = html.slice(html.indexOf("<script>") + 8, html.lastIndexOf("</script>"));
// Stop before startup fetch while retaining the production bootstrap order.
// Anonymous sessions now recover an expired cookie before rendering.
const bootstrap = script.indexOf('api("/api/health").catch(');
assert.notEqual(bootstrap, -1);
assert.ok(script.indexOf("resumeSavedRevisit(BOOTSTRAP_RESUME_TOKEN)", bootstrap) <
  script.indexOf("resumeAcceptedThesis(BOOTSTRAP_RESUME_TOKEN)", bootstrap));
const context = vm.createContext({
  document, localStorage: storage, sessionStorage: storage, window,
  fetch, URLSearchParams, crypto: { randomUUID: () => "7a574f4c-fb79-4394-9704-a71883469539" }, console,
});
vm.runInContext(script.slice(0, bootstrap), context);

const id = {
  thesis: "69084356-67ac-473d-81a0-ceb58784444b",
  normalization: "96609920-04b6-4bd2-ac12-dd6ac5411675",
  sourceChoice: "4be54218-0c83-4e3e-8fed-f94b8d39c5aa",
  display: "65462d41-dff7-4537-a761-6a62f0a6777b",
  choice: "968e6568-ced7-4f31-b2d5-ab721a629bc3",
  assessment: "753e5410-e2cb-4b48-836e-c6cd4def1fb4",
  capture: "c432a9e2-b937-43be-aa5a-58bd75a6c22d",
  personal: "9bd1aac8-d801-4d47-aabf-caaaf47605ec",
  revision: "752075ec-ec28-47c8-8380-789d1212d900",
  observation: "c75ea6b9-2d3f-4346-8ea6-b1f5ac03333b",
  laterCapture: "79d05bac-d698-46ce-8cf6-4e9f4b39f97d",
  retainedCapture: "d53b8297-c561-460d-a557-142e507d311d",
  packet: "49105e64-b9c1-4fed-b546-99699b7fa287",
  decision: "dd4fa817-6d39-4a20-86f2-a4fdb09d457d",
};
const originalRules = "The market resolves YES if Harbor publishes audited reserve notes.";
const laterRules = originalRules + " <img src=x onerror=alert(1)> Added audit condition.";
const thesis = "Harbor reserve notes exceed $200 billion by 2028.";
const title = "Harbor reserve notes market";
const snapshotDate = "2026-09-18T12:00:00+00:00";

function json(body, status = 200) {
  return Promise.resolve({ ok: status < 400, status, json: async () => body });
}
function descendants(node, predicate) {
  const matches = [];
  for (const child of node.children || []) {
    if (predicate(child)) matches.push(child);
    matches.push(...descendants(child, predicate));
  }
  return matches;
}
function byClass(node, name) {
  return descendants(node, (child) =>
    typeof child.className === "string" && child.className.split(" ").includes(name));
}
function visibleText(node) {
  if (node.tagName === "DETAILS" && !node.open) {
    return node.children[0]?.textContent || "";
  }
  return (node._textContent || "") +
    (node.children || []).map(visibleText).join("");
}
function binding(overrides = {}) {
  return {
    personal_thesis_id: id.personal,
    revision_id: id.revision,
    revision_digest: "revision-digest",
    thesis_analysis_id: id.thesis,
    normalization_decision_id: id.normalization,
    source_candidate_choice_id: id.sourceChoice,
    market_display_set_id: id.display,
    market_choice_id: id.choice,
    market_assessment_id: id.assessment,
    market_id: "harbor-market",
    baseline_snapshot_id: "snapshot-one",
    baseline_rules_capture_id: id.capture,
    baseline_rules_digest: "rules-hash-one",
    ...overrides,
  };
}
function activation(overrides = {}) {
  return {
    binding: binding(),
    baseline: {
      accepted_thesis: thesis,
      source_excerpt: "Harbor's reserve note publication plan",
      title,
      rules: originalRules,
      title_hash: "title-hash-one",
      rules_hash: "rules-hash-one",
      deadline: null,
      lifecycle: null,
      snapshot_as_of: snapshotDate,
      source_kind: "frozen_market_snapshot",
    },
    active: true,
    registered_captures: [],
    ...overrides,
  };
}
function check(result = "revisit_proposed", overrides = {}) {
  const changed = result === "revisit_proposed";
  return {
    binding: binding(),
    observation: {
      id: id.observation,
      capture_id: id.laterCapture,
      capture_digest: "capture-hash-two",
      result,
      triggers: changed ? ["rules_changed"] : [],
      unknowns: ["baseline_deadline_unknown", "baseline_lifecycle_unknown"],
      unchanged: changed ? ["title_unchanged"] : ["title_unchanged", "rules_unchanged"],
      before: {
        title, rules: originalRules, deadline: null, lifecycle: null,
        title_hash: "title-hash-one", rules_hash: "rules-hash-one",
      },
      after: {
        title, rules: changed ? laterRules : originalRules,
        deadline: null, lifecycle: null,
        title_hash: "title-hash-one",
        rules_hash: changed ? "rules-hash-two" : "rules-hash-one",
      },
      source_kind: "synthetic_fixture",
      source_ref: "fixture:rules_added",
      observed_at: "2026-09-19T12:00:00+00:00",
      receipt_at: "2026-09-19T12:01:00+00:00",
      policy_version: "manual-revisit-v0",
    },
    packet: changed ? { id: id.packet, digest: "packet-digest" } : null,
    decision: null,
    ...overrides,
  };
}
function savedActivation() {
  const bound = binding({
    revision_digest: "a".repeat(64),
    baseline_rules_digest: "b".repeat(64),
  });
  const original = activation();
  return {
    ...original,
    binding: bound,
    baseline: {
      ...original.baseline,
      market_id: bound.market_id,
      title_hash: "c".repeat(64),
      rules_hash: bound.baseline_rules_digest,
      condition_id: null,
    },
  };
}
function savedCheck(decision = null) {
  const original = check();
  const bound = savedActivation().binding;
  return {
    ...original,
    binding: bound,
    observation: {
      ...original.observation,
      before: {
        ...original.observation.before,
        title_hash: "c".repeat(64),
        rules_hash: bound.baseline_rules_digest,
      },
    },
    decision,
  };
}
const fixtureResolutionEffect = "The new rule allows extra time to correct obvious data errors. It is unclear whether a correction can cancel an earlier reading of 60 or more: another rule says qualifying readings still count after revision.";
const fixtureFitEffect = "A delay alone may leave the fit unchanged. If a qualifying reading can be rejected, the market may become a weaker expression of your thesis.";
function fixtureImpactNote(result) {
  const observation = result.observation;
  return {
    version: "fixture-possible-impact-v1",
    approval_scope: "copy_only_not_semantic_adjudication",
    provenance: "Owner-approved fixture explanation",
    binding: { ...result.binding },
    observation_id: observation.id,
    capture_id: observation.capture_id,
    capture_digest: observation.capture_digest,
    source_kind: observation.source_kind,
    source_ref: observation.source_ref,
    policy_version: observation.policy_version,
    packet_id: result.packet.id,
    packet_digest: result.packet.digest,
    before_rules_hash: observation.before.rules_hash,
    after_rules_hash: observation.after.rules_hash,
    resolution_effect: fixtureResolutionEffect,
    fit_effect: fixtureFitEffect,
  };
}
function prepare(kind = "market") {
  vm.runInContext("resetDownstream()", context);
  vm.runInContext(`
    chain.thesisId = "${id.thesis}";
    chain.normalizationDecisionId = "${id.normalization}";
    chain.sourceCandidateChoiceId = "${id.sourceChoice}";
    chain.marketDisplaySetId = "${id.display}";
    chain.marketSnapshotId = "snapshot-one";
    chain.marketSnapshotAsOf = "${snapshotDate}";
    chain.marketCards = [{
      market_assessment_id: "${id.assessment}", market_id: "harbor-market",
      market_title: "${title}", resolution_conditions: ${JSON.stringify(originalRules)},
      rules_capture_id: "${id.capture}"
    }];
    chain.selectionKind = "${kind}";
    chain.selectedAssessmentId = ${kind === "market" ? `"${id.assessment}"` : "null"};
    chain.selectedMarket = ${kind === "market" ? "chain.marketCards[0]" : "null"};
    $("norm-summary").textContent = "${thesis}";
    $("norm-summary").dataset.acceptedSummary = "${thesis}";
  `, context);
  $("submit-choice").disabled = false;
}
async function recordChoice(kind = "market") {
  prepare(kind);
  respond = (path) => {
    assert.equal(path, `/api/v3/market-pools/${id.display}/choice`);
    return json({
      market_choice_id: id.choice,
      market_display_set_id: id.display,
      selection_kind: kind,
      market_assessment_id: kind === "market" ? id.assessment : null,
    });
  };
  await $("submit-choice").onclick();
}
async function activate() {
  respond = (path) => {
    if (path.endsWith("/revisit-activation")) return json(activation());
    assert.equal(path, `/api/v3/thesis-revisions/${id.revision}/revisit`);
    return json({ activation: activation(), latest: null });
  };
  await $("activate-revisit").onclick();
}

async function fullAcceptedWordingDoesNotBreakCanonicalRevisitBinding() {
  await recordChoice("market");
  $("norm-summary").textContent = "Harbor publishes reserve notes or demonstrates the accepted alternative.";
  assert.notEqual($("norm-summary").textContent, thesis);
  await activate();
  assert.equal(vm.runInContext("chain.revisitBinding.revision_id", context), id.revision);
  assert.equal($("revisit-thesis").textContent, thesis);
}
function selectScenario(value) {
  $("revisit-fixture-scenario").value = value;
  $("revisit-fixture-scenario").listeners.get("change")();
}

async function noneChoiceIsExplicitlyUnavailable() {
  await recordChoice("none");
  assert.equal($("revisit-followup").hidden, false);
  assert.equal($("activate-revisit").hidden, true);
  assert.match($("revisit-eligibility").textContent, /unavailable.*none choice/);
}

async function choiceMismatchCannotUnlockFollowup() {
  prepare("market");
  respond = () => json({
    market_choice_id: id.choice, market_display_set_id: id.display,
    selection_kind: "market", market_assessment_id: id.capture,
  });
  await $("submit-choice").onclick();
  assert.equal($("revisit-followup").hidden, true);
  assert.equal($("activate-revisit").disabled, true);
  assert.match($("s3-err").textContent, /identity mismatch/);
}

async function activationAndReadProjectionStayBound() {
  await recordChoice();
  assert.equal($("activate-revisit").disabled, false);
  vm.runInContext('productMode = "fixture"', context);
  await activate();
  assert.equal($("revisit-active").hidden, false);
  assert.equal($("revisit-resume-link").hidden, false);
  assert.equal($("revisit-resume-link").href,
    `/?resume_revisit_revision_id=${id.revision}#revisit-followup`);
  assert.equal($("revisit-source").textContent,
    "Harbor's reserve note publication plan");
  assert.equal($("revisit-thesis").textContent, thesis);
  assert.equal($("revisit-baseline-date").textContent, snapshotDate);
  assert.equal($("revisit-baseline-source").textContent,
    "Frozen baseline evidence · snapshot-one");
  assert.match($("revisit-check-mode").textContent, /Synthetic saved-evidence replay/);
  assert.equal($("revisit-fixture-controls").hidden, false);
  assert.equal($("run-revisit-check").disabled, true);
}

async function anonymousDemoWithoutLaterCaptureHasNoSyntheticAction() {
  vm.runInContext('productMode = "fixture"; anonymousDemo = true;', context);
  await recordChoice();
  await activate();
  assert.equal($("revisit-fixture-controls").hidden, true);
  assert.equal($("revisit-saved-controls").hidden, true);
  assert.equal($("run-revisit-check").disabled, true);
  assert.match($("revisit-check-mode").textContent,
    /no later saved capture.*retained-rules journey/i);
  // A stale synthetic selection must not unlock an unsupported demo request.
  const before = calls.length;
  selectScenario("rules_added");
  assert.equal($("run-revisit-check").disabled, true);
  await $("run-revisit-check").onclick();
  assert.equal(calls.length, before);
  vm.runInContext('anonymousDemo = false;', context);
}

async function anonymousDemoRetainedCaptureCanRetryAndRefresh() {
  vm.runInContext('productMode = "fixture"; anonymousDemo = true;', context);
  await recordChoice();
  const retained = {
    capture_id: id.retainedCapture, source_kind: "retained_capture",
    observed_at: "2026-09-19T12:00:00+00:00",
    receipt_at: "2026-09-19T12:01:00+00:00",
  };
  const saved = activation({ registered_captures: [retained] });
  respond = (path) => path.endsWith("/revisit-activation")
    ? json(saved) : json({ activation: saved, latest: null });
  await $("activate-revisit").onclick();
  assert.equal($("revisit-fixture-controls").hidden, true);
  assert.equal($("revisit-fixture-scenario").value, "");
  assert.equal($("revisit-saved-controls").hidden, false);
  assert.doesNotMatch($("revisit-check-mode").textContent, /replay case/);
  $("revisit-saved-capture").value = id.retainedCapture;
  $("revisit-saved-capture").listeners.get("change")();
  assert.equal($("run-revisit-check").disabled, false);
  const payloads = [];
  let release;
  respond = (_path, options) => {
    payloads.push(JSON.parse(options.body));
    return new Promise((resolve) => { release = resolve; });
  };
  const pending = $("run-revisit-check").onclick();
  assert.equal($("run-revisit-check").disabled, true);
  assert.equal($("revisit-check-pending").hidden, false);
  release({ ok: false, status: 503, json: async () => ({ detail: "Retry saved evidence" }) });
  await pending;
  assert.equal($("revisit-check-pending").hidden, true);
  assert.equal($("run-revisit-check").disabled, false);
  assert.match($("revisit-error").textContent, /Retry saved evidence/);
  assert.equal($("submit-revisit-decision").disabled, true);
  respond = (_path, options) => {
    payloads.push(JSON.parse(options.body));
    return json(check("incomplete"));
  };
  await $("run-revisit-check").onclick();
  assert.deepEqual(payloads[1], payloads[0]);
  assert.equal(payloads[0].capture_id, id.retainedCapture);
  assert.equal(Object.hasOwn(payloads[0], "fixture_scenario"), false);
  assert.equal($("revisit-error").hidden, true);
  assert.equal($("revisit-check-pending").hidden, true);
  respond = () => json({ activation: saved, latest: null });
  await $("refresh-revisit").onclick();
  assert.equal($("run-revisit-check").disabled, true);
  assert.equal($("revisit-fixture-controls").hidden, true);
  assert.equal($("revisit-saved-capture").value, "");
  vm.runInContext('anonymousDemo = false;', context);
}

async function mismatchedActivationFailsClosed() {
  await recordChoice();
  respond = () => json(activation({ binding: binding({ market_assessment_id: id.capture }) }));
  await $("activate-revisit").onclick();
  assert.equal($("revisit-active").hidden, true);
  assert.equal($("submit-revisit-decision").disabled, true);
  assert.match($("revisit-error").textContent, /identity mismatch/);
}

async function changedRulesAreReadableAndDecisionBound() {
  await recordChoice();
  vm.runInContext('productMode = "fixture"', context);
  await activate();
  selectScenario("rules_added");
  assert.equal($("run-revisit-check").disabled, false);
  respond = (path, options) => {
    assert.equal(path, `/api/v3/thesis-revisions/${id.revision}/revisit-check`);
    assert.equal(JSON.parse(options.body).fixture_scenario, "rules_added");
    const checked = check();
    checked.fixture_possible_impact = fixtureImpactNote(checked);
    return json(checked);
  };
  await $("run-revisit-check").onclick();
  assert.equal($("revisit-result").hidden, false);
  assert.equal($("revisit-result-heading").textContent, "Revisit proposed");
  assert.match($("revisit-later-source").textContent, /Synthetic saved-evidence replay/);
  assert.match($("revisit-unknowns").textContent, /original capture did not retain a scheduled deadline/);
  assert.doesNotMatch($("revisit-unknowns").textContent, /baseline_deadline_unknown/);
  assert.match($("revisit-result-explanation").textContent, /resolution rules changed/);
  assert.equal($("revisit-changes").children.length, 1);
  assert.match(byClass($("revisit-changes"), "revisit-diff-add")[0].textContent,
    /\+ Added.*Added audit condition/);
  assert.equal($("revisit-impact-heading").textContent, "Why review this change?");
  assert.equal($("revisit-impact-provenance").hidden, true);
  assert.match($("revisit-impact-resolution").textContent,
    /may change how or when this contract resolves/);
  assert.match($("revisit-impact-fit").textContent,
    /no new expression class was assigned/);
  assert.equal($("revisit-impact-caveat").textContent,
    "No case-specific explanation is available. This comparison does not reassess the market.");
  assert.equal($("revisit-changes").children[0].innerHTML, "");
  assert.equal($("revisit-after-rules-text").textContent, laterRules);
  assert.equal($("revisit-before-rules-text").textContent, originalRules);
  assert.match(byClass($("revisit-after-rules-text"), "revisit-full-add")[0].textContent,
    /<img src=x onerror=alert\(1\)> Added audit condition/);
  assert.equal(descendants($("revisit-result"), (child) => child.tagName === "IMG").length, 0);
  assert.equal($("revisit-before-rules").open, false);
  assert.equal($("revisit-after-rules").open, false);
  assert.equal($("revisit-disposition").hidden, false);
  assert.equal($("revisit-decision-state").textContent, "unanswered");
  assert.equal($("submit-revisit-decision").disabled, true);

  decisionRadios[1].checked = true;
  decisionRadios[1].listeners.get("change")();
  respond = (path, options) => {
    assert.equal(path, `/api/v3/revisit-packets/${id.packet}/decision`);
    assert.deepEqual(JSON.parse(options.body), {
      action: "reaffirm", expected_revision_digest: "revision-digest",
      expected_observation_id: id.observation,
    });
    return json({
      binding: binding(), packet_id: id.packet, observation_id: id.observation,
      decision_id: id.decision, action: "reaffirm",
      decision_origin: "human_ui", created_at: "2026-09-20T00:00:00+00:00",
    });
  };
  await $("submit-revisit-decision").onclick();
  assert.equal($("revisit-decision-state").textContent, "decision recorded");
  assert.equal($("revisit-decision-ack").hidden, false);
  assert.equal($("revisit-disposition").hidden, true);
  assert.equal(decisionRadios[1].disabled, true);

  respond = (path) => {
    assert.equal(path, `/api/v3/thesis-revisions/${id.revision}/revisit`);
    return json({ activation: activation(), latest: check("revisit_proposed", {
      decision: {
        binding: binding(), packet_id: id.packet, observation_id: id.observation,
        decision_id: id.decision, action: "reaffirm", decision_origin: "human_ui",
        created_at: "2026-09-20T00:00:00+00:00",
      },
    }) });
  };
  await $("refresh-revisit").onclick();
  assert.equal($("revisit-disposition").hidden, true);
  assert.match($("revisit-result-explanation").textContent, /already recorded/);
  assert.equal($("revisit-decision-ack").hidden, false);
}

async function renderRuleComparison(original, later, hasRuleTrigger = true) {
  await recordChoice();
  vm.runInContext('productMode = "fixture"', context);
  await activate();
  context.testOriginalRules = original;
  vm.runInContext("chain.revisitBaseline.rules = testOriginalRules", context);
  const result = check(hasRuleTrigger ? "revisit_proposed" : "incomplete");
  result.observation.before.rules = original;
  result.observation.after.rules = later;
  result.observation.triggers = hasRuleTrigger ? ["rules_changed"] : [];
  result.observation.unchanged = hasRuleTrigger ? ["title_unchanged"] : [];
  context.testRuleResult = result;
  vm.runInContext("renderRevisitResult(testRuleResult)", context);
}

async function longAppendedRuleIsVisibleWithoutOpeningFullText() {
  const paragraph = "In case of obvious data integrity issues (i.e., erroneous data), " +
    "the market may remain open until the end of the third calendar day (ET) " +
    "after the date on which such data is first released to allow for corrections. " +
    "Data integrity issues refer only to clerical or other similar errors in " +
    "the underlying data, and do not include cases where IMF Portwatch differs " +
    "from alternative sources.";
  const intro = "This market will resolve to Yes if IMF Portwatch publishes a 7-day " +
    "moving average of transit calls for the Strait of Hormuz at least 60. " +
    "Otherwise it resolves No.";
  const retained = "Revisions to previously published data points made within this " +
    "market's timeframe will be considered. " + "Same context. ".repeat(25);
  const source = "The resolution source for this market will be IMF Portwatch.";
  const original = [intro, retained, source].join("\n\n");
  const later = [intro, retained, paragraph, source].join("\n\n");
  assert.ok(original.length > 360 && paragraph.length > 320);
  await renderRuleComparison(original, later);
  const diff = byClass($("revisit-changes"), "revisit-rule-diff")[0];
  const additions = byClass(diff, "revisit-diff-add");
  assert.ok(additions.some((line) => line.textContent.includes(paragraph)));
  assert.equal(byClass(diff, "revisit-diff-remove").length, 0);
  assert.ok(byClass(diff, "revisit-diff-context").some((detail) => !detail.open));
  assert.doesNotMatch(visibleText(diff).slice(0, 160), /This market will resolve/);
  assert.equal($("revisit-after-rules").open, false);
  assert.equal($("revisit-before-rules-text").textContent, original);
  assert.equal($("revisit-after-rules-text").textContent, later);
  assert.equal(byClass($("revisit-after-rules-text"), "revisit-full-add")
    .map((span) => span.textContent).join(""), paragraph + "\n\n");
}

async function replacedRuleTextIsLabeledLiterally() {
  await renderRuleComparison(
    "Same opening. Old review term. Same ending.",
    "Same opening. New review term. Same ending.");
  const diff = byClass($("revisit-changes"), "revisit-rule-diff")[0];
  assert.match(byClass(diff, "revisit-diff-remove")[0].textContent, /− Removed.*Old/);
  assert.match(byClass(diff, "revisit-diff-add")[0].textContent, /\+ Added.*New/);
  assert.equal(byClass($("revisit-before-rules-text"), "revisit-full-remove")[0].textContent,
    "Old");
  assert.equal(byClass($("revisit-after-rules-text"), "revisit-full-add")[0].textContent,
    "New");
}

async function separatedChangesAndDeletionStaySeparate() {
  const before = "Start\nOld first clause.\nUnchanged middle one.\nUnchanged middle two.\nDelete this clause.\nUnchanged bridge.\nOld final clause.\nEnd";
  const after = "Start\nNew first clause.\nUnchanged middle one.\nUnchanged middle two.\nUnchanged bridge.\nNew final clause.\nEnd";
  await renderRuleComparison(before, after);
  const diff = byClass($("revisit-changes"), "revisit-rule-diff")[0];
  const removed = byClass(diff, "revisit-diff-remove").map((line) => line.textContent);
  const added = byClass(diff, "revisit-diff-add").map((line) => line.textContent);
  assert.equal(removed.length, 3);
  assert.equal(added.length, 2);
  assert.match(removed[0], /Old first clause/);
  assert.match(removed[1], /Delete this clause/);
  assert.match(removed[2], /Old final clause/);
  assert.match(added[0], /New first clause/);
  assert.match(added[1], /New final clause/);
  assert.ok(diff.textContent.indexOf("Unchanged middle") >
    diff.textContent.indexOf("Old first clause"));
  assert.ok(diff.textContent.indexOf("Unchanged middle") <
    diff.textContent.indexOf("Delete this clause"));
  assert.equal($("revisit-before-rules-text").textContent, before);
  assert.equal($("revisit-after-rules-text").textContent, after);
}

async function overboundDiffShowsExactFullTextFallback() {
  const before = Array.from({ length: 245 }, (_, i) => `Original line ${i}`).join("\n");
  const after = before + "\nOne more rule.";
  await renderRuleComparison(before, after);
  assert.match($("revisit-changes").textContent, /exceeds the safe display limit/);
  assert.equal(byClass($("revisit-changes"), "revisit-rule-diff").length, 0);
  assert.equal($("revisit-before-rules-text").textContent, before);
  assert.equal($("revisit-after-rules-text").textContent, after);
  assert.equal($("revisit-rules-impact").hidden, false);
}

async function blankLinesUnicodeAndTrailingNewlineRemainExact() {
  const before = "Café ⚓\n\nRule 🔒\n";
  const after = "Café ⚓\n\n\nRule 🔒 updated\n";
  await renderRuleComparison(before, after);
  const additions = byClass($("revisit-changes"), "revisit-diff-add");
  assert.ok(additions.some((line) => /\+ Added\(blank line\)/.test(line.textContent)));
  assert.ok(additions.some((line) => line.textContent.includes("updated")));
  assert.equal($("revisit-before-rules-text").textContent, before);
  assert.equal($("revisit-after-rules-text").textContent, after);
  assert.ok(byClass($("revisit-after-rules-text"), "revisit-full-add").length > 0);
}

async function finalLineBreakOnlyIsVisibleAndHighlighted() {
  await renderRuleComparison("Rule 🔒", "Rule 🔒\n");
  assert.match(byClass($("revisit-changes"), "revisit-diff-add")[0].textContent,
    /\+ Added\(line break at end\)/);
  assert.equal($("revisit-before-rules-text").textContent, "Rule 🔒");
  assert.equal($("revisit-after-rules-text").textContent, "Rule 🔒\n");
  assert.equal(byClass($("revisit-after-rules-text"), "revisit-full-add")[0].textContent,
    "\n");
  await renderRuleComparison("Rule 🔒\n", "Rule 🔒");
  assert.match(byClass($("revisit-changes"), "revisit-diff-remove")[0].textContent,
    /− Removed\(line break at end\)/);
  assert.equal($("revisit-before-rules-text").textContent, "Rule 🔒\n");
  assert.equal($("revisit-after-rules-text").textContent, "Rule 🔒");
  assert.equal(byClass($("revisit-before-rules-text"), "revisit-full-remove")[0].textContent,
    "\n");
}

async function noRuleTriggerDoesNotImplyLiteralChange() {
  await renderRuleComparison("Original rule.", "Original rule.\nUnverified later text.", false);
  assert.equal($("revisit-changes").children.length, 0);
  assert.match($("revisit-changes").textContent, /No comparable field change/);
  assert.equal($("revisit-rules-impact").hidden, true);
  assert.equal($("revisit-after-rules-text").textContent,
    "Original rule.\nUnverified later text.");
}

async function crossPanelDecisionMismatchFailsClosed() {
  await recordChoice();
  vm.runInContext('productMode = "fixture"', context);
  await activate();
  respond = () => json({ activation: activation(), latest: check("revisit_proposed", {
    decision: {
      binding: binding({ market_id: "another-market" }),
      packet_id: id.packet, observation_id: id.observation,
      decision_id: id.decision, action: "dismiss", decision_origin: "human_ui",
    },
  }) });
  await $("refresh-revisit").onclick();
  assert.equal($("revisit-result").hidden, true);
  assert.equal($("submit-revisit-decision").disabled, true);
  assert.equal($("revisit-rules-impact").hidden, true);
  assert.equal($("revisit-changes").children.length, 0);
  assert.match($("revisit-error").textContent, /identity mismatch/);
}

async function incompleteObservationCannotOfferDisposition() {
  await recordChoice();
  vm.runInContext('productMode = "fixture"', context);
  await activate();
  selectScenario("unchanged");
  respond = () => json(check("incomplete"));
  await $("run-revisit-check").onclick();
  assert.equal($("revisit-result-heading").textContent, "Comparison incomplete");
  assert.equal($("revisit-disposition").hidden, true);
  assert.equal($("submit-revisit-decision").disabled, true);
}

async function registeredRetainedCaptureUsesServerIdOnly() {
  await recordChoice();
  vm.runInContext('productMode = "gemini"', context);
  const registered = {
    capture_id: id.retainedCapture, source_kind: "retained_capture",
    observed_at: "2026-09-19T12:00:00+00:00",
    receipt_at: "2026-09-19T12:01:00+00:00",
  };
  const savedActivation = activation({ registered_captures: [registered] });
  respond = (path) => path.endsWith("/revisit-activation")
    ? json(savedActivation)
    : json({ activation: savedActivation, latest: null });
  await $("activate-revisit").onclick();
  assert.equal($("revisit-fixture-controls").hidden, true);
  assert.equal($("revisit-saved-controls").hidden, false);
  assert.equal($("revisit-saved-capture").children[1].value, id.retainedCapture);
  assert.match($("revisit-saved-capture").children[1].textContent,
    /Registered retained capture/);
  $("revisit-saved-capture").value = id.retainedCapture;
  $("revisit-saved-capture").listeners.get("change")();
  assert.equal($("run-revisit-check").disabled, false);
  respond = (path, options) => {
    assert.equal(path, `/api/v3/thesis-revisions/${id.revision}/revisit-check`);
    const payload = JSON.parse(options.body);
    assert.equal(payload.capture_id, id.retainedCapture);
    assert.equal(Object.hasOwn(payload, "fixture_scenario"), false);
    assert.equal(Object.hasOwn(payload, "rules"), false);
    const result = check("incomplete");
    result.observation.source_kind = "retained_capture";
    result.request_capture = {
      id: id.retainedCapture, digest: "request-capture-hash",
      source_kind: "retained_capture", observed_at: registered.observed_at,
    };
    return json(result);
  };
  await $("run-revisit-check").onclick();
  assert.match($("revisit-later-source").textContent, /Registered retained capture/);
  assert.match($("revisit-request-capture").textContent, /first recorded evidence/);
  assert.equal($("revisit-disposition").hidden, true);
}

async function sourceEditDropsLateCheck() {
  await recordChoice();
  vm.runInContext('productMode = "fixture"', context);
  await activate();
  selectScenario("rules_added");
  let release;
  respond = () => new Promise((resolve) => { release = resolve; });
  const pending = $("run-revisit-check").onclick();
  vm.runInContext("resetDownstream()", context);
  release({ ok: true, status: 200, json: async () => check() });
  await pending;
  assert.equal($("revisit-followup").hidden, true);
  assert.equal($("revisit-result").hidden, true);
  assert.equal($("submit-revisit-decision").disabled, true);
  assert.equal($("revisit-rules-impact").hidden, true);
  assert.equal($("revisit-changes").children.length, 0);
}

async function sourceEditClearsRenderedDiffAndNote() {
  await renderRuleComparison("Original rule.", "Original rule.\nAdded rule.");
  assert.equal($("revisit-rules-impact").hidden, false);
  assert.ok(byClass($("revisit-changes"), "revisit-diff-add").length > 0);
  $("input_text").value = "Another source";
  $("input_text").listeners.get("input")();
  assert.equal($("revisit-followup").hidden, true);
  assert.equal($("revisit-rules-impact").hidden, true);
  assert.equal($("revisit-changes").children.length, 0);
  assert.equal($("revisit-after-rules-text").textContent, "");
}

async function fullReloadRestoresPendingPacketReadOnly() {
  vm.runInContext("resetDownstream()", context);
  assert.equal($("s1-fresh-controls").hidden, false);
  assert.equal($("s3-discovery-controls").hidden, false);
  assert.equal($("saved-revisit-source-panel").hidden, true);
  assert.equal($("saved-revisit-market-panel").hidden, true);
  vm.runInContext('productMode = "fixture"', context);
  window.location.search = `?resume_revisit_revision_id=${id.revision}&resume_thesis_id=${id.thesis}`;
  const before = calls.length;
  const scrollBefore = $("revisit-followup").scrollCount;
  respond = (path, options) => {
    assert.equal(path, `/api/v3/thesis-revisions/${id.revision}/revisit`);
    assert.equal(options.method, undefined);
    return json({ activation: savedActivation(), latest: savedCheck() });
  };
  assert.equal(await vm.runInContext("resumeSavedRevisit()", context), true);
  assert.equal(calls.length, before + 1);
  assert.equal($("revisit-followup").hidden, false);
  assert.equal($("revisit-active").hidden, false);
  assert.equal($("revisit-result-heading").textContent, "Revisit proposed");
  assert.equal($("revisit-rules-impact").hidden, false);
  assert.match($("revisit-changes").textContent, /\+ Added/);
  assert.equal($("revisit-source").textContent,
    "Harbor's reserve note publication plan");
  assert.equal($("revisit-thesis").textContent, thesis);
  assert.equal($("revisit-market").textContent, `${title} · market harbor-market`);
  assert.equal($("revisit-baseline-date").textContent, snapshotDate);
  assert.equal($("revisit-activation-ack").textContent,
    "Restored saved follow-up. No new acceptance, choice, or activation was recorded.");
  assert.equal($("activate-revisit").hidden, true);
  assert.equal($("submit-revisit-decision").disabled, true);
  assert.equal($("revisit-decision-state").textContent, "unanswered");
  assert.equal($("s1-out").hidden, true);
  assert.equal($("pool").hidden, true);
  assert.equal($("s1-fresh-controls").hidden, true);
  assert.equal($("s3-discovery-controls").hidden, true);
  assert.equal($("saved-revisit-source-panel").hidden, false);
  assert.equal($("saved-revisit-market-panel").hidden, false);
  assert.equal($("s1-heading").textContent, "Saved source and thesis");
  assert.equal($("s1-tag").textContent, "read-only context");
  assert.equal($("s3-heading").textContent, "Saved selected contract");
  assert.equal($("s3-tag").textContent, "frozen baseline rules");
  assert.equal($("saved-revisit-source").textContent,
    "Harbor's reserve note publication plan");
  assert.equal($("saved-revisit-thesis").textContent, thesis);
  assert.equal($("saved-revisit-market-title").textContent, title);
  assert.equal($("saved-revisit-market-id").textContent, "harbor-market");
  assert.equal($("saved-revisit-market-date").textContent, snapshotDate);
  assert.equal($("saved-revisit-market-rules").textContent, originalRules);
  assert.equal($("saved-revisit-market-rules-hash").textContent, "b".repeat(64));
  assert.deepEqual(JSON.parse($("saved-revisit-source-binding").textContent),
    JSON.parse(JSON.stringify(savedActivation().binding)));
  assert.equal($("saved-revisit-market-binding").textContent,
    $("saved-revisit-source-binding").textContent);
  assert.equal($("revisit-intro").textContent,
    "Compare retained contract evidence with its frozen baseline. Reopening preserves the saved records.");
  assert.equal($("saved-revisit-source-panel").dataset.binding,
    $("saved-revisit-market-panel").dataset.binding);
  assert.equal(JSON.parse($("saved-revisit-source-panel").dataset.binding).revision_digest,
    "a".repeat(64));
  assert.equal($("run").disabled, true);
  assert.equal($("find-markets").disabled, true);
  assert.equal(vm.runInContext("chain.thesisId", context), null);
  assert.equal(vm.runInContext("chain.marketChoiceId", context), null);
  assert.equal($("revisit-resume-link").href,
    `/?resume_revisit_revision_id=${id.revision}#revisit-followup`);
  assert.equal($("revisit-followup").scrollCount, scrollBefore + 1);

  decisionRadios[0].checked = true;
  decisionRadios[0].listeners.get("change")();
  assert.equal($("submit-revisit-decision").disabled, false);
  respond = (path, options) => {
    assert.equal(path, `/api/v3/revisit-packets/${id.packet}/decision`);
    assert.deepEqual(JSON.parse(options.body), {
      action: "dismiss", expected_revision_digest: "a".repeat(64),
      expected_observation_id: id.observation,
    });
    return json({
      binding: savedActivation().binding, packet_id: id.packet,
      observation_id: id.observation, decision_id: id.decision,
      action: "dismiss", decision_origin: "human_ui",
      created_at: "2026-09-20T00:00:00+00:00",
    });
  };
  await $("submit-revisit-decision").onclick();
  assert.equal($("revisit-decision-ack").hidden, false);
  assert.equal($("revisit-disposition").hidden, true);
}

async function revisionDraftIsLocalBoundAndCancelable() {
  vm.runInContext("resetDownstream()", context);
  window.location.search = `?resume_revisit_revision_id=${id.revision}`;
  respond = () => json({ activation: savedActivation(), latest: savedCheck() });
  await vm.runInContext("resumeSavedRevisit()", context);
  const before = calls.length;
  decisionRadios[0].checked = true;
  decisionRadios[0].listeners.get("change")();
  assert.equal($("submit-revisit-decision").disabled, false);
  $("open-revisit-draft").onclick();
  assert.equal(calls.length, before);
  assert.equal($("revisit-revision-draft").hidden, false);
  assert.equal($("revisit-draft-current").textContent, thesis);
  assert.equal($("revisit-draft-text").value, thesis);
  assert.equal($("revisit-after-rules-text").textContent, laterRules);
  assert.equal(JSON.parse($("revisit-revision-draft").dataset.binding).revision_digest,
    "a".repeat(64));
  assert.equal($("revisit-revision-draft").dataset.observationId, id.observation);
  assert.equal($("revisit-revision-draft").dataset.packetId, id.packet);
  assert.equal($("revisit-revision-draft").dataset.packetDigest, "packet-digest");
  assert.equal(decisionRadios[0].checked, false);
  assert.equal(decisionRadios[0].disabled, true);
  assert.equal(decisionRadios[1].disabled, true);
  assert.equal($("submit-revisit-decision").disabled, true);
  assert.equal($("revisit-decision-state").textContent, "unanswered");
  decisionRadios[1].checked = true;
  decisionRadios[1].listeners.get("change")();
  assert.equal(decisionRadios[1].checked, false);
  await $("submit-revisit-decision").onclick();
  assert.equal(calls.length, before);
  $("revisit-draft-text").value = "My proposed successor wording";
  $("cancel-revisit-draft").onclick();
  assert.equal(calls.length, before);
  assert.equal($("revisit-revision-draft").hidden, true);
  assert.equal($("revisit-draft-current").textContent, "");
  assert.equal($("revisit-draft-text").value, "");
  assert.equal($("revisit-revision-draft").dataset.binding, "");
  assert.equal(decisionRadios[0].disabled, false);
  assert.equal(decisionRadios[1].disabled, false);
  assert.equal($("submit-revisit-decision").disabled, true);
}

async function newPacketClearsLocalRevisionDraft() {
  vm.runInContext("resetDownstream()", context);
  window.location.search = `?resume_revisit_revision_id=${id.revision}`;
  respond = () => json({ activation: savedActivation(), latest: savedCheck() });
  await vm.runInContext("resumeSavedRevisit()", context);
  $("open-revisit-draft").onclick();
  $("revisit-draft-text").value = "Unaccepted local edit";
  const newer = savedCheck();
  newer.observation.id = id.capture;
  newer.packet = { id: id.decision, digest: "new-packet-digest" };
  respond = () => json({ activation: savedActivation(), latest: newer });
  await $("refresh-revisit").onclick();
  assert.equal($("revisit-revision-draft").hidden, true);
  assert.equal($("revisit-draft-text").value, "");
  assert.equal($("revisit-revision-draft").dataset.packetId, "");
  assert.equal(vm.runInContext("chain.revisitPacketId", context), id.decision);
  assert.equal($("revisit-decision-state").textContent, "unanswered");
}

async function recordedDecisionRestoresWithoutPrompt() {
  vm.runInContext("resetDownstream()", context);
  window.location.search = `?resume_revisit_revision_id=${id.revision}`;
  const decision = {
    binding: savedActivation().binding,
    packet_id: id.packet, observation_id: id.observation,
    decision_id: id.decision, action: "dismiss", decision_origin: "human_ui",
    created_at: "2026-09-20T00:00:00+00:00",
  };
  const latest = savedCheck(decision);
  latest.fixture_possible_impact = fixtureImpactNote(latest);
  respond = () => json({ activation: savedActivation(), latest });
  await vm.runInContext("resumeSavedRevisit()", context);
  assert.equal($("revisit-disposition").hidden, true);
  assert.match($("revisit-decision-ack").textContent, /dismiss/);
  assert.equal($("submit-revisit-decision").disabled, true);
  assert.equal($("revisit-impact-heading").textContent,
    "Possible impact — requires review");
  assert.equal($("saved-revisit-source-panel").hidden, false);
  assert.equal($("saved-revisit-market-panel").hidden, false);
  assert.equal($("open-revisit-draft").disabled, true);
  $("open-revisit-draft").onclick();
  assert.equal($("revisit-revision-draft").hidden, true);
}

async function exactFixtureImpactIsCopyOnlyAndReadOnly() {
  vm.runInContext("resetDownstream()", context);
  window.location.search = `?resume_revisit_revision_id=${id.revision}`;
  const latest = savedCheck();
  latest.fixture_possible_impact = fixtureImpactNote(latest);
  const before = calls.length;
  respond = (path, options) => {
    assert.equal(path, `/api/v3/thesis-revisions/${id.revision}/revisit`);
    assert.equal(options.method, undefined);
    return json({ activation: savedActivation(), latest });
  };
  await vm.runInContext("resumeSavedRevisit()", context);
  assert.equal(calls.length, before + 1);
  assert.equal($("revisit-impact-heading").textContent,
    "Possible impact — requires review");
  assert.equal($("revisit-impact-provenance").textContent,
    "Owner-approved fixture explanation");
  assert.equal($("revisit-impact-provenance").hidden, false);
  assert.equal($("revisit-impact-resolution").textContent, fixtureResolutionEffect);
  assert.equal($("revisit-impact-fit").textContent, fixtureFitEffect);
  assert.equal($("revisit-impact-caveat").textContent,
    "This explanation describes possible effects. The market has not been reassessed.");
  assert.equal($("revisit-impact-resolution").innerHTML, "");
  assert.equal($("revisit-disposition").hidden, false);
  assert.equal($("submit-revisit-decision").disabled, true);
  assert.equal(vm.runInContext("chain.thesisId", context), null);
}

async function mismatchedFixtureImpactFallsBackToGeneric() {
  const mismatches = [
    (note) => { note.binding.revision_digest = "wrong"; },
    (note) => { note.observation_id = id.packet; },
    (note) => { note.packet_digest = "wrong"; },
    (note) => { note.before_rules_hash = "wrong"; },
    (note) => { note.after_rules_hash = "wrong"; },
    (note) => { note.source_kind = "synthetic_fixture_wrong"; },
    (note) => { note.approval_scope = "semantic_assessment"; },
  ];
  for (const mutate of mismatches) {
    vm.runInContext("resetDownstream()", context);
    window.location.search = `?resume_revisit_revision_id=${id.revision}`;
    const latest = savedCheck();
    latest.fixture_possible_impact = fixtureImpactNote(latest);
    mutate(latest.fixture_possible_impact);
    respond = () => json({ activation: savedActivation(), latest });
    await vm.runInContext("resumeSavedRevisit()", context);
    assert.equal($("revisit-impact-heading").textContent,
      "Why review this change?");
    assert.equal($("revisit-impact-provenance").hidden, true);
    assert.equal($("revisit-impact-resolution").textContent.includes(
      fixtureResolutionEffect), false);
    assert.match($("revisit-impact-fit").textContent,
      /no new expression class was assigned/);
    assert.equal($("revisit-disposition").hidden, false);
    assert.equal($("submit-revisit-decision").disabled, true);
  }
  vm.runInContext("resetDownstream()", context);
  vm.runInContext('productMode = "gemini"', context);
  window.location.search = `?resume_revisit_revision_id=${id.revision}`;
  const latest = savedCheck();
  latest.fixture_possible_impact = fixtureImpactNote(latest);
  respond = () => json({ activation: savedActivation(), latest });
  await vm.runInContext("resumeSavedRevisit()", context);
  assert.equal($("revisit-impact-heading").textContent,
    "Why review this change?");
  vm.runInContext('productMode = "fixture"', context);
}

async function mismatchedRefreshDropsPriorFixtureCopy() {
  vm.runInContext("resetDownstream()", context);
  window.location.search = `?resume_revisit_revision_id=${id.revision}`;
  const latest = savedCheck();
  latest.fixture_possible_impact = fixtureImpactNote(latest);
  respond = () => json({ activation: savedActivation(), latest });
  await vm.runInContext("resumeSavedRevisit()", context);
  assert.equal($("revisit-impact-resolution").textContent, fixtureResolutionEffect);
  const changed = savedCheck();
  changed.fixture_possible_impact = fixtureImpactNote(changed);
  changed.fixture_possible_impact.packet_digest = "not-the-saved-packet";
  respond = () => json({ activation: savedActivation(), latest: changed });
  await $("refresh-revisit").onclick();
  assert.equal($("revisit-impact-heading").textContent,
    "Why review this change?");
  assert.equal($("revisit-impact-provenance").hidden, true);
  assert.equal($("revisit-impact-resolution").textContent.includes(
    fixtureResolutionEffect), false);
  assert.equal($("revisit-disposition").hidden, false);
}

async function sourceEditClearsFixtureImpactWithDiff() {
  vm.runInContext("resetDownstream()", context);
  window.location.search = `?resume_revisit_revision_id=${id.revision}`;
  const latest = savedCheck();
  latest.fixture_possible_impact = fixtureImpactNote(latest);
  respond = () => json({ activation: savedActivation(), latest });
  await vm.runInContext("resumeSavedRevisit()", context);
  assert.equal($("revisit-impact-heading").textContent,
    "Possible impact — requires review");
  $("input_text").value = "Fresh source";
  $("input_text").listeners.get("input")();
  assert.equal($("revisit-rules-impact").hidden, true);
  assert.equal($("revisit-impact-resolution").textContent, "");
  assert.equal($("revisit-impact-fit").textContent, "");
  assert.equal($("revisit-changes").children.length, 0);
  assert.equal($("submit-revisit-decision").disabled, true);
}

async function nullableReplayContextStaysLiteralAndReadOnly() {
  for (const sourceExcerpt of [null, "  \n"]) {
    vm.runInContext("resetDownstream()", context);
    window.location.search = `?resume_revisit_revision_id=${id.revision}`;
    const saved = savedActivation();
    saved.baseline.source_excerpt = sourceExcerpt;
    saved.baseline.rules = "Archived rule <img src=x onerror=alert(1)>\n\nEnd ⚓";
    respond = () => json({ activation: saved, latest: null });
    const before = calls.length;
    await vm.runInContext("resumeSavedRevisit()", context);
    assert.equal(calls.length, before + 1);
    assert.match($("saved-revisit-source").textContent, /No saved source context/);
    assert.equal($("saved-revisit-market-rules").textContent, saved.baseline.rules);
    assert.equal($("saved-revisit-market-rules").innerHTML, "");
    assert.equal($("saved-revisit-thesis").textContent, thesis);
    assert.equal($("s1-out").hidden, true);
    assert.equal($("pool").hidden, true);
    assert.equal(vm.runInContext("chain.thesisId", context), null);
    assert.equal(vm.runInContext("chain.marketChoiceId", context), null);
  }
}

async function newSourceEscapeClearsProjectionWithoutFetch() {
  vm.runInContext("resetDownstream()", context);
  window.location.search = `?resume_revisit_revision_id=${id.revision}`;
  const disclosure = "TEST-ONLY HISTORICAL REPLAY. Saved acceptance and selection are test setup, not new human decisions. The baseline is reconstructed from an archived description for this replay; it is not a prior product snapshot.";
  const saved = savedActivation();
  saved.baseline.source_excerpt = disclosure;
  respond = () => json({ activation: saved, latest: savedCheck() });
  await vm.runInContext("resumeSavedRevisit()", context);
  assert.equal($("saved-revisit-source").textContent, disclosure);
  assert.equal($("saved-revisit-source-panel").hidden, false);
  const before = calls.length;
  const focusBefore = $("input_text").focusCount;
  $("start-new-source").onclick();
  assert.equal(calls.length, before);
  assert.equal($("input_text").focusCount, focusBefore + 1);
  assert.equal($("s1-fresh-controls").hidden, false);
  assert.equal($("s3-discovery-controls").hidden, false);
  assert.equal($("run").disabled, false);
  assert.equal($("find-markets").disabled, false);
  assert.equal($("saved-revisit-source-panel").hidden, true);
  assert.equal($("saved-revisit-market-panel").hidden, true);
  assert.equal($("saved-revisit-source").textContent, "");
  assert.equal($("saved-revisit-market-rules").textContent, "");
  assert.equal($("revisit-followup").hidden, true);
  assert.equal($("revisit-changes").children.length, 0);
  assert.equal($("s1-heading").textContent, "Source intake");
  assert.equal($("s3-tag").textContent, "up to three independent assessments");
  assert.match($("revisit-intro").textContent, /Activation is a separate human decision/);
  assert.equal(window.location.search.includes("resume_revisit_revision_id"), false);
}

async function failedRefreshClearsAllSavedCards() {
  vm.runInContext("resetDownstream()", context);
  window.location.search = `?resume_revisit_revision_id=${id.revision}`;
  const latest = savedCheck();
  latest.fixture_possible_impact = fixtureImpactNote(latest);
  respond = () => json({ activation: savedActivation(), latest });
  await vm.runInContext("resumeSavedRevisit()", context);
  assert.equal($("saved-revisit-source-panel").hidden, false);
  assert.equal($("revisit-impact-heading").textContent,
    "Possible impact — requires review");
  $("open-revisit-draft").onclick();
  $("revisit-draft-text").value = "Unaccepted local edit";
  assert.equal($("revisit-revision-draft").hidden, false);
  respond = () => json({ error: "revisit_conflict" }, 409);
  await $("refresh-revisit").onclick();
  assert.equal($("saved-revisit-source-panel").hidden, true);
  assert.equal($("saved-revisit-market-panel").hidden, true);
  assert.equal($("saved-revisit-source").textContent, "");
  assert.equal($("saved-revisit-market-rules").textContent, "");
  assert.equal($("revisit-active").hidden, true);
  assert.equal($("revisit-rules-impact").hidden, true);
  assert.equal($("revisit-impact-resolution").textContent, "");
  assert.equal($("revisit-revision-draft").hidden, true);
  assert.equal($("revisit-draft-text").value, "");
  assert.equal($("submit-revisit-decision").disabled, true);
  assert.equal($("revisit-error").hidden, false);
}

async function sourceEditDropsLateRefreshAndAllSavedCards() {
  vm.runInContext("resetDownstream()", context);
  window.location.search = `?resume_revisit_revision_id=${id.revision}`;
  respond = () => json({ activation: savedActivation(), latest: savedCheck() });
  await vm.runInContext("resumeSavedRevisit()", context);
  $("open-revisit-draft").onclick();
  $("revisit-draft-text").value = "Unaccepted local edit";
  let release;
  respond = () => new Promise((resolve) => { release = resolve; });
  const pending = $("refresh-revisit").onclick();
  $("input_text").value = "Fresh source";
  $("input_text").listeners.get("input")();
  release({ ok: true, status: 200, json: async () => ({
    activation: savedActivation(), latest: savedCheck(),
  }) });
  await pending;
  assert.equal($("saved-revisit-source-panel").hidden, true);
  assert.equal($("saved-revisit-market-panel").hidden, true);
  assert.equal($("saved-revisit-source").textContent, "");
  assert.equal($("revisit-revision-draft").hidden, true);
  assert.equal($("revisit-draft-text").value, "");
  assert.equal($("revisit-followup").hidden, true);
  assert.equal($("s1-fresh-controls").hidden, false);
  assert.equal(window.location.search.includes("resume_revisit_revision_id"), false);
}

async function malformedOrForeignReadFailsClosed() {
  vm.runInContext("resetDownstream()", context);
  window.location.search = "?resume_revisit_revision_id=not-a-uuid";
  const before = calls.length;
  const scrollBefore = $("revisit-followup").scrollCount;
  assert.equal(await vm.runInContext("resumeSavedRevisit()", context), true);
  assert.equal(calls.length, before);
  assert.equal($("revisit-active").hidden, true);
  assert.equal($("submit-revisit-decision").disabled, true);
  assert.equal($("revisit-error").hidden, false);
  assert.equal($("revisit-followup").scrollCount, scrollBefore);
  assert.equal($("saved-revisit-source-panel").hidden, true);
  assert.equal($("saved-revisit-market-panel").hidden, true);

  for (const response of [
    { activation: savedActivation(), latest: savedCheck({
      binding: binding({ market_id: "foreign-market" }),
      packet_id: id.packet, observation_id: id.observation,
      decision_id: id.decision, action: "dismiss", decision_origin: "human_ui",
    }) },
    { activation: { ...savedActivation(), binding: {
      ...savedActivation().binding, revision_id: id.packet,
    } }, latest: null },
  ]) {
    vm.runInContext("resetDownstream()", context);
    window.location.search = `?resume_revisit_revision_id=${id.revision}`;
    respond = () => json(response);
    await vm.runInContext("resumeSavedRevisit()", context);
    assert.equal($("revisit-active").hidden, true);
    assert.equal($("revisit-result").hidden, true);
    assert.equal($("submit-revisit-decision").disabled, true);
    assert.equal($("revisit-error").hidden, false);
    assert.equal($("revisit-followup").scrollCount, scrollBefore);
    assert.equal($("saved-revisit-source-panel").hidden, true);
    assert.equal($("saved-revisit-market-panel").hidden, true);
  }
  vm.runInContext("resetDownstream()", context);
  window.location.search = `?resume_revisit_revision_id=${id.revision}`;
  respond = () => json({ error: "revisit_not_found" }, 404);
  await vm.runInContext("resumeSavedRevisit()", context);
  assert.equal($("revisit-active").hidden, true);
  assert.equal($("revisit-error").hidden, false);
  assert.equal($("revisit-followup").scrollCount, scrollBefore);
  assert.equal($("saved-revisit-source-panel").hidden, true);
  assert.equal($("saved-revisit-market-panel").hidden, true);
}

async function sourceEditDropsLateReloadResponse() {
  vm.runInContext("resetDownstream()", context);
  window.location.search = `?resume_revisit_revision_id=${id.revision}`;
  let release;
  const scrollBefore = $("revisit-followup").scrollCount;
  respond = () => new Promise((resolve) => { release = resolve; });
  const pending = vm.runInContext("resumeSavedRevisit()", context);
  $("input_text").value = "New source journey";
  $("input_text").listeners.get("input")();
  release({ ok: true, status: 200, json: async () => ({
    activation: savedActivation(), latest: savedCheck(),
  }) });
  await pending;
  assert.equal($("revisit-followup").hidden, true);
  assert.equal($("revisit-result").hidden, true);
  assert.equal($("submit-revisit-decision").disabled, true);
  assert.equal(vm.runInContext("chain.revisitResumed", context), false);
  assert.equal(window.location.search.includes("resume_revisit_revision_id"), false);
  assert.equal($("revisit-followup").scrollCount, scrollBefore);
  assert.equal($("saved-revisit-source-panel").hidden, true);
  assert.equal($("saved-revisit-market-panel").hidden, true);
}

noneChoiceIsExplicitlyUnavailable()
  .then(fullAcceptedWordingDoesNotBreakCanonicalRevisitBinding)
  .then(choiceMismatchCannotUnlockFollowup)
  .then(activationAndReadProjectionStayBound)
  .then(anonymousDemoWithoutLaterCaptureHasNoSyntheticAction)
  .then(anonymousDemoRetainedCaptureCanRetryAndRefresh)
  .then(mismatchedActivationFailsClosed)
  .then(changedRulesAreReadableAndDecisionBound)
  .then(longAppendedRuleIsVisibleWithoutOpeningFullText)
  .then(replacedRuleTextIsLabeledLiterally)
  .then(separatedChangesAndDeletionStaySeparate)
  .then(overboundDiffShowsExactFullTextFallback)
  .then(blankLinesUnicodeAndTrailingNewlineRemainExact)
  .then(finalLineBreakOnlyIsVisibleAndHighlighted)
  .then(noRuleTriggerDoesNotImplyLiteralChange)
  .then(crossPanelDecisionMismatchFailsClosed)
  .then(incompleteObservationCannotOfferDisposition)
  .then(registeredRetainedCaptureUsesServerIdOnly)
  .then(sourceEditDropsLateCheck)
  .then(sourceEditClearsRenderedDiffAndNote)
  .then(fullReloadRestoresPendingPacketReadOnly)
  .then(revisionDraftIsLocalBoundAndCancelable)
  .then(newPacketClearsLocalRevisionDraft)
  .then(recordedDecisionRestoresWithoutPrompt)
  .then(exactFixtureImpactIsCopyOnlyAndReadOnly)
  .then(mismatchedFixtureImpactFallsBackToGeneric)
  .then(mismatchedRefreshDropsPriorFixtureCopy)
  .then(sourceEditClearsFixtureImpactWithDiff)
  .then(nullableReplayContextStaysLiteralAndReadOnly)
  .then(newSourceEscapeClearsProjectionWithoutFetch)
  .then(failedRefreshClearsAllSavedCards)
  .then(sourceEditDropsLateRefreshAndAllSavedCards)
  .then(malformedOrForeignReadFailsClosed)
  .then(sourceEditDropsLateReloadResponse)
  .catch((error) => { console.error(error); process.exitCode = 1; });
