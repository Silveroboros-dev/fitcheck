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
    this.dataset = {};
    this.style = {};
    this.className = "";
    this.listeners = new Map();
    this.attributes = new Map();
    this.classList = {
      add: () => {},
      remove: () => {},
      toggle: () => {},
    };
  }
  addEventListener(name, callback) { this.listeners.set(name, callback); }
  append() {}
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
  createTextNode: () => new Element(),
  querySelectorAll: () => [],
};
const storage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
const window = { location: { search: "" } };
const fetchCalls = [];
let response = null;
const fetch = (path, opts) => {
  fetchCalls.push({ path, opts });
  return response(path, opts);
};

const html = fs.readFileSync(process.argv[2], "utf8");
const script = html.slice(html.indexOf("<script>") + 8, html.lastIndexOf("</script>"));
const end = script.indexOf('$("choose-none").addEventListener');
assert.notEqual(end, -1, "market Find handler must remain before market choice wiring");
const context = vm.createContext({
  document, localStorage: storage, sessionStorage: storage, fetch, window,
  URLSearchParams, crypto: { randomUUID: () => "test-id" }, console,
});
vm.runInContext(script.slice(0, end), context);

const acceptedThesisId = "d17cb421-3aad-4685-834c-138c8bbd0ba3";
const decisionId = "b8c315b3-407a-4775-98e2-8dbab97e31e2";
const originalSource = "Harbor settlement tokens may increase Northland reserve notes by 2028. A separate note concerns Orchard Exchange.";
const selectedQuote = "Harbor settlement tokens may increase Northland reserve notes by 2028.";

function restoredSource(overrides = {}) {
  return {
    thesis_analysis_id: acceptedThesisId,
    normalization_decision_id: decisionId,
    source_interpretation_request_id: "8089c30e-4028-4c19-999f-c9a6eef60521",
    source_interpretation_id: "526875a3-b59b-4484-a337-ed17c4a0ac8f",
    source_thesis_candidate_id: "aa874678-4909-4094-8d24-daf55612be37",
    source_candidate_choice_id: "4f11b055-f30e-4481-a262-70e2201d909a",
    original_source_text: originalSource,
    selected_source_quote: selectedQuote,
    accepted_normalization_input: selectedQuote + " Human clarification: by 2028.",
    source_url: null,
    ...overrides,
  };
}

function acceptedState(overrides = {}) {
  return {
    thesis_analysis_id: acceptedThesisId,
    accepted_thesis_summary: "Stablecoin inflows exceed USD 200 billion by 2028.",
    normalization_decision_id: decisionId,
    accepted_at: "2026-09-18T02:41:00+00:00",
    acceptance_origin: "human_ui",
    restored_source: restoredSource(),
    ...overrides,
  };
}

function jsonResponse(body) {
  return Promise.resolve({ ok: true, status: 200, json: async () => body });
}

function prepareResume() {
  window.location.search = "?resume_thesis_id=" + acceptedThesisId;
  vm.runInContext("resetDownstream();", context);
  $("s1-err").hidden = true;
  $("s1-err").textContent = "";
}

function editSource(text) {
  $("input_text").value = text;
  const onInput = $("input_text").listeners.get("input");
  assert.equal(typeof onInput, "function", "the source field must have a runtime input handler");
  onInput();
}

async function restoresAcceptedThesisWithoutAutoFindingMarkets() {
  prepareResume();
  const before = fetchCalls.length;
  response = (path) => {
    assert.equal(path, `/api/v3/theses/${acceptedThesisId}/accepted-state`);
    return jsonResponse(acceptedState());
  };

  await vm.runInContext("resumeAcceptedThesis()", context);

  assert.equal(fetchCalls.length, before + 1);
  assert.equal(vm.runInContext("chain.thesisId", context), acceptedThesisId);
  assert.equal($("norm-summary").textContent, acceptedState().accepted_thesis_summary);
  assert.equal($("s1-out").hidden, false);
  assert.equal($("restored-acceptance").hidden, false);
  assert.match($("restored-acceptance").textContent, /no new decision was recorded/);
  assert.equal($("restored-source").hidden, false);
  assert.equal($("restored-source-note").hidden, false);
  assert.equal($("restored-source-text").textContent, originalSource);
  assert.equal($("restored-source-quote").textContent, "Selected excerpt: " + selectedQuote);
  assert.equal($("restored-source-normalization-input").hidden, false);
  assert.match($("restored-source-normalization-input").textContent, /Human clarification/);
  assert.equal($("input_text").value, "", "restoration never writes into new-source input");
  assert.equal($("find-markets").disabled, false);
  assert.equal(vm.runInContext("chain.sourceInterpretationId", context), null);
  assert.equal(vm.runInContext("chain.normalizationAttemptId", context), null);
  assert.equal(
    fetchCalls.slice(before).some((call) => call.path.includes("/market-pool")),
    false,
    "restoring acceptance must not dispatch Find"
  );
}

async function wrongIdentityOrOriginLeavesJourneyUnbound() {
  for (const invalid of [
    acceptedState({ thesis_analysis_id: "0f4e9974-e401-4e94-9db7-8b8285e8cb64" }),
    acceptedState({ acceptance_origin: "agent_mcp" }),
    acceptedState({ restored_source: restoredSource({
      normalization_decision_id: "0f4e9974-e401-4e94-9db7-8b8285e8cb64"
    }) }),
    acceptedState({ restored_source: restoredSource({ accepted_normalization_input: "" }) }),
  ]) {
    prepareResume();
    const before = fetchCalls.length;
    response = (path) => {
      assert.equal(path, `/api/v3/theses/${acceptedThesisId}/accepted-state`);
      return jsonResponse(invalid);
    };

    await vm.runInContext("resumeAcceptedThesis()", context);

    assert.equal(fetchCalls.length, before + 1);
    assert.equal(vm.runInContext("chain.thesisId", context), null);
    assert.equal($("s1-out").hidden, true);
    assert.equal($("restored-source").hidden, true);
    assert.match($("s1-err").textContent, /Saved accepted thesis is unavailable/);
  }
}

async function legacyAcceptanceShowsExplicitUnavailableSource() {
  prepareResume();
  response = () => jsonResponse(acceptedState({ restored_source: null }));
  await vm.runInContext("resumeAcceptedThesis()", context);
  assert.equal($("restored-source").hidden, false);
  assert.match($("restored-source-text").textContent, /unavailable/);
  assert.equal($("restored-source-quote").hidden, true);
  assert.equal($("restored-source-normalization-input").hidden, true);
}

async function maliciousSourceIsInertAndAnEditClearsTheRestoration() {
  prepareResume();
  const maliciousSource = '<img src=x onerror=alert(1)> ' + originalSource;
  response = () => jsonResponse(acceptedState({
    restored_source: restoredSource({ original_source_text: maliciousSource }),
  }));
  await vm.runInContext("resumeAcceptedThesis()", context);

  assert.equal($("restored-source-text").textContent, maliciousSource);
  assert.equal($("restored-source-text").innerHTML, "",
    "source content must be assigned as inert text, not parsed HTML");
  assert.equal($("s1-out").hidden, false);

  editSource("A new source journey");
  assert.equal(vm.runInContext("chain.thesisId", context), null);
  assert.equal($("s1-out").hidden, true);
  assert.equal($("restored-source").hidden, true);
  assert.equal($("restored-source-text").textContent, "");
  assert.equal($("restored-source-note").hidden, true);
}

async function sourceEditDropsPendingResumeResponse() {
  prepareResume();
  let release;
  response = () => new Promise((resolve) => { release = resolve; });
  const before = fetchCalls.length;
  const pending = vm.runInContext("resumeAcceptedThesis()", context);
  assert.equal(fetchCalls.length, before + 1);

  editSource("A different source");
  release({ ok: true, status: 200, json: async () => acceptedState() });
  await pending;

  assert.equal(vm.runInContext("chain.thesisId", context), null);
  assert.equal($("s1-out").hidden, true);
  assert.equal($("restored-acceptance").hidden, true);
  assert.equal($("restored-source").hidden, true);
  assert.equal($("restored-source-text").textContent, "");
  assert.equal($("restored-source-normalization-input").textContent, "");
  assert.equal(fetchCalls.length, before + 1);
}

async function sourceEditBeforeHealthCompletesCancelsBootstrapResume() {
  prepareResume();
  const bootstrapToken = vm.runInContext("chain.requestToken", context);
  editSource("A newer source");
  const before = fetchCalls.length;
  response = () => {
    throw new Error("a canceled bootstrap resume must not fetch accepted state");
  };

  await vm.runInContext(`resumeAcceptedThesis(${bootstrapToken})`, context);

  assert.equal(fetchCalls.length, before);
  assert.equal(vm.runInContext("chain.thesisId", context), null);
  assert.equal($("s1-out").hidden, true);
}

restoresAcceptedThesisWithoutAutoFindingMarkets()
  .then(wrongIdentityOrOriginLeavesJourneyUnbound)
  .then(legacyAcceptanceShowsExplicitUnavailableSource)
  .then(maliciousSourceIsInertAndAnEditClearsTheRestoration)
  .then(sourceEditDropsPendingResumeResponse)
  .then(sourceEditBeforeHealthCompletesCancelsBootstrapResume)
  .catch((error) => {
    console.error(error);
    process.exitCode = 1;
  });
