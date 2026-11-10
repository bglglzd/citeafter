const $ = (id) => document.getElementById(id);
const GATES = {
  allowed: ["Ready for preview", "good"],
  stale: ["Recheck required", "warning"],
  needs_review: ["Needs review", "review"],
  source_unavailable: ["Source unavailable", "bad"],
  released: ["Released", "neutral"],
};
const IMPACTS = {
  affected: ["Affected", "warning"],
  unaffected: ["Unchanged support", "good"],
  needs_review: ["Needs review", "review"],
};
const HEALTH = {
  fresh: ["Fresh source", "good"],
  expired: ["Source expired", "warning"],
  error: ["Fetch failed", "bad"],
  empty: ["No source", "neutral"],
};
const REASONS = {
  explicit_import: "Policy imported locally.",
  http_200: "Policy received from the pinned source.",
  http_304: "The pinned source confirmed the cached policy is unchanged.",
  no_successful_source_check: "Fetch or import a policy before continuing.",
  source_check_expired: "The source check expired. Fetch the source again.",
  required_business_value_changed: "A required policy field changed. Recheck this decision.",
  changed_required_business_value: "A required policy field changed.",
  supported_current_values: "Current policy values support this saved result.",
  support_requires_review: "Source support needs manual review before release.",
  original_outbox_receipt_retained: "The original outbox receipt is retained.",
  selected_version_not_covered_by_latest_check: "The latest source check does not cover the selected version.",
  manual_dependency_coverage_unproven: "Manual pointers do not prove complete dependency coverage.",
  dependency_coverage_unproven: "Complete dependency coverage has not been established.",
  missing_dependency: "A required policy field is missing.",
  unsupported_clause_kind: "This recipe does not support the selected clause kind.",
  nonempty_normative_text_requires_review: "The clause contains additional text that requires manual review.",
  unsupported_input_currency: "The input currency does not match the policy clause.",
  unsupported_input_destination: "The input destination does not match the policy clause.",
  mixed_policy_families: "The recorded sources belong to different policy families.",
  no_effective_known_source: "No known source applies at the selected business time.",
  conflicting_version_identity: "Different source bytes use the same version label.",
  cyclic_lineage: "The policy version history contains a cycle.",
  missing_or_conflicting_known_parent: "A parent policy version is missing or ambiguous.",
  incomparable_effective_versions: "More than one policy branch applies at the selected time.",
  unique_effective_lineage: "One recorded policy lineage applies at the selected time.",
  conditional_cache_changed: "The conditional cache changed during the request. Fetch the source again.",
  import_byte_limit_prefix_only: "The import exceeded 256 KiB. Only its bounded rejected prefix was retained.",
  wrong_policy: "The source belongs to a different policy family.",
  invalid_url: "The source URL is outside the supported pinned endpoint profile.",
  configuration: "The source fetch limits are invalid.",
  etag: "The source validator is invalid or does not match the conditional request.",
  redirect: "The source returned a redirect. Redirects are not followed.",
  http_status: "The source did not return a supported successful status.",
  content_type: "The source must provide UTF-8 JSON content.",
  content_encoding: "The source used an unsupported content encoding.",
  framing: "The source response was incomplete or used unsupported framing.",
  too_large: "The source exceeded the configured byte limit.",
  utf8: "The source response is not valid UTF-8.",
  json: "The source response is not a supported JSON document.",
  timeout: "The source fetch timed out.",
  tls: "The source TLS check failed.",
  network: "The source connection could not complete.",
  worker: "The isolated source fetch could not complete.",
};
const model = {
  state: null, recorded: null, connected: false, busy: false,
  actionId: null, sourceSha: null, highlight: null, preview: null,
};
const isObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
const list = (value) => Array.isArray(value) ? value : [];
const text = (value) => value === null || value === undefined ? "—" : String(value);
const title = (value) => text(value).replaceAll("_", " ").replace(/^./, (c) => c.toUpperCase());
const reasonText = (code) => Object.hasOwn(REASONS, code) ? REASONS[code] : "Inspect the assessment details before continuing.";

function element(tag, attributes = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attributes)) {
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = text(value);
    else if (key === "disabled") node.disabled = Boolean(value);
    else node.setAttribute(key, String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function button(label, handler, options = {}) {
  const node = element("button", { type: "button", class: "button secondary", text: label, ...options });
  node.addEventListener("click", handler);
  return node;
}

function badge(entry) {
  const [label, tone] = entry ?? ["Not assessed", "neutral"];
  return element("span", { class: "badge " + tone, text: label });
}

function timeNode(value) {
  if (!value) return document.createTextNode("Not recorded");
  const date = new Date(value);
  const shown = Number.isNaN(date.valueOf()) ? text(value) : new Intl.DateTimeFormat("en-GB", {
    day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", timeZone: "UTC",
  }).format(date) + " UTC";
  return element("time", { datetime: value, title: value, text: shown });
}

function fact(label, value) {
  return element("div", {}, element("dt", { text: label }), element("dd", {}, value));
}

function valueNode(value) {
  return isObject(value) || Array.isArray(value)
    ? element("pre", { text: JSON.stringify(value, null, 2) })
    : document.createTextNode(text(value));
}

function valueGrid(value) {
  if (!isObject(value)) return element("p", { class: "empty-state", text: value === null || value === undefined ? "No result available." : text(value) });
  return element("dl", { class: "value-grid" }, Object.entries(value).map(([key, item]) => fact(title(key), valueNode(item))));
}

function rawDetails(label, value) {
  return element("details", { class: "raw-details" }, element("summary", { text: label }), element("pre", { text: JSON.stringify(value, null, 2) }));
}

function currentAction() {
  return model.state?.actions.find((action) => action.id === model.actionId) ?? null;
}

function live() {
  return !model.recorded && model.connected && ["local", "synthetic-local"].includes(model.state?.mode)
    && typeof model.state?.csrf_token === "string" && model.state.csrf_token.length > 0;
}

function announce(message) {
  $("status-message").textContent = message;
}

function showError(error) {
  let message = error instanceof Error ? error.message : text(error);
  const token = model.state?.csrf_token;
  if (token) message = message.replaceAll(token, "[redacted]");
  $("error-message").textContent = message;
  $("error-message").hidden = false;
}

function clearError() {
  $("error-message").hidden = true;
  $("error-message").textContent = "";
}

function clearPreview() {
  model.preview = null;
  $("release-preview").disabled = true;
  if ($("preview-dialog").open) $("preview-dialog").close();
}

function updateControls() {
  const action = currentAction();
  $("workspace").setAttribute("aria-busy", String(model.busy));
  $("reload-state").disabled = model.busy || Boolean(model.recorded);
  $("refresh-source").disabled = model.busy || !live();
  $("decision-search").disabled = model.busy || !model.state;
  $("decision-filter").disabled = model.busy || !model.state;
  $("recorded-stage").disabled = model.busy;
  for (const node of document.querySelectorAll(".decision-select")) node.disabled = model.busy;
  for (const id of ["history-valid", "history-known"]) $(id).disabled = model.busy || Boolean(model.recorded) || !action;
  $("inspect-history").disabled = model.busy || !live() || !action;
  const recheck = $("recheck-action");
  if (recheck) recheck.disabled = model.busy || !live() || !canRecheck(action);
  const preview = $("preview-action");
  if (preview) preview.disabled = model.busy || !live() || action?.assessment.gate !== "allowed" || model.state.health.status !== "fresh";
  $("release-preview").disabled = model.busy || !live() || !model.preview;
  if (model.state && !model.recorded && !model.connected) $("mode-label").textContent = "Local connection unavailable";
  for (const node of document.querySelectorAll("[data-demo-version]")) {
    node.disabled = model.busy || !live() || model.state.sources.some((source) => source.version_id === node.dataset.demoVersion);
  }
}

function canRecheck(action) {
  return Boolean(action && !action.receipt && action.assessment.gate === "stale"
    && action.assessment.impact_class === "affected" && action.claim.result.result_status === "supported");
}

function initialAction(actions) {
  const pending = actions.filter((action) => !action.receipt && action.assessment.gate !== "released");
  return pending.find(canRecheck) ?? pending.find((action) => action.assessment.gate === "allowed")
    ?? pending.find((action) => action.assessment.gate === "needs_review") ?? pending[0] ?? actions[0] ?? null;
}

async function requestJSON(path, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetch(path, { credentials: "same-origin", mode: "same-origin", cache: "no-store", redirect: "error", ...options, signal: controller.signal });
    let data;
    try { data = await response.json(); }
    catch {
      const error = new Error("The local response could not be read as JSON. Reload state before continuing.");
      error.httpStatus = response.status; error.invalidResponse = true; throw error;
    }
    if (!response.ok) {
      const error = new Error(typeof data?.error?.message === "string" ? data.error.message : "The local request was refused.");
      error.code = data?.error?.code ?? "request_refused";
      error.httpStatus = response.status;
      throw error;
    }
    return data;
  } catch (error) {
    if (error.name === "AbortError" || error instanceof TypeError) {
      const connectionError = new Error(error.name === "AbortError" ? "The local request timed out. Reload state to inspect the outcome." : "The local connection is unavailable. Reload state before continuing.");
      connectionError.isConnectionError = true; throw connectionError;
    }
    throw error;
  } finally { clearTimeout(timer); }
}

function validateState(state, recorded = false) {
  if (!isObject(state) || !Array.isArray(state.sources) || !Array.isArray(state.actions)
    || !isObject(state.health) || !Object.hasOwn(HEALTH, state.health.status)) throw new Error("The workspace state format is invalid.");
  if (!recorded && (!["local", "synthetic-local"].includes(state.mode) || typeof state.csrf_token !== "string" || !state.csrf_token)) {
    throw new Error("The local workspace did not provide a valid session. Reload state.");
  }
  if (state.actions.some((action) => !isObject(action) || typeof action.id !== "string" || !isObject(action.claim?.result)
    || !isObject(action.assessment) || !Object.hasOwn(GATES, action.assessment.gate) || !Object.hasOwn(IMPACTS, action.assessment.impact_class))) throw new Error("A decision record is incomplete.");
  if (state.sources.some((source) => !isObject(source) || typeof source.sha256 !== "string" || typeof source.rendered_text !== "string")) throw new Error("A source record is incomplete.");
  return state;
}

function applyState(state) {
  model.state = validateState(state, Boolean(model.recorded));
  model.connected = !model.recorded;
  if (!currentAction()) model.actionId = initialAction(state.actions)?.id ?? null;
  const action = currentAction();
  if (!state.sources.some((source) => source.sha256 === model.sourceSha)) model.sourceSha = action?.claim.result.source_sha256 ?? state.sources[0]?.sha256 ?? null;
  model.highlight = null;
  render();
}

async function reloadState() {
  const state = await requestJSON("/api/state");
  clearPreview();
  applyState(state);
}

async function post(path, data) {
  if (!live()) throw new Error("This view is read-only. Open the local workspace to perform this action.");
  return requestJSON(path, { method: "POST", headers: { "Content-Type": "application/json", "X-Citeafter-Token": model.state.csrf_token }, body: JSON.stringify(data) });
}

async function task(label, operation) {
  if (model.busy) return;
  model.busy = true;
  clearError(); announce(label); updateControls();
  try { await operation(); }
  catch (error) {
    if (error.isConnectionError || error.invalidResponse) { model.connected = false; clearPreview(); }
    showError(error); announce("Request stopped. Inspect the message before continuing.");
  }
  finally { model.busy = false; updateControls(); }
}

async function mutate(path, data, message) {
  if (!live()) throw new Error("This view is read-only. Open the local workflow to perform this action.");
  clearPreview();
  try {
    await post(path, data);
    await reloadState();
    announce(message);
  } catch (error) {
    try { await reloadState(); }
    catch { model.connected = false; updateControls(); }
    throw error;
  }
}

function renderHealth() {
  const state = model.state;
  $("health-panel").hidden = false;
  const [label, tone] = HEALTH[state.health.status];
  $("health-badge").className = "badge " + tone;
  $("health-badge").textContent = label + (model.recorded ? " · recorded" : "");
  $("health-reason").textContent = state.health.reason ? reasonText(state.health.reason) : "";
  $("health-checked").replaceChildren(timeNode(state.health.checked_at));
  const seconds = (value) => Number.isFinite(value) ? `${Math.round(value)} s` : "—";
  $("health-age").textContent = `${seconds(state.health.age_seconds)} / ${seconds(state.health.ttl_seconds)}`;
  $("workspace-now").replaceChildren(timeNode(state.now));
}

function renderSources() {
  const sources = model.state.sources;
  $("source-list").replaceChildren(...sources.map((source) => {
    const node = button(source.version_id, () => {
      model.sourceSha = source.sha256; model.highlight = null; renderSources(); $("source-text-details").open = true;
    }, { class: "source-choice", "aria-pressed": String(source.sha256 === model.sourceSha) });
    return node;
  }));
  const source = sources.find((item) => item.sha256 === model.sourceSha);
  if (!source) {
    $("source-metadata").replaceChildren(element("p", { class: "empty-state", text: "No source has been observed." }));
    $("source-text").textContent = ""; $("source-text-details").hidden = true; return;
  }
  $("source-text-details").hidden = false;
  $("source-metadata").replaceChildren(
    element("dl", { class: "source-meta" }, fact("Effective from", timeNode(source.effective_from)), fact("Observed at", timeNode(source.observed_at)), fact("Parent", source.parent ?? "Root version")),
    element("details", { class: "identity-details" }, element("summary", { text: "Source identity" }),
      element("dl", {}, fact("Received bytes · SHA-256", source.sha256), fact("Rendered text · SHA-256", source.render_sha256))),
  );
  if (!model.recorded && model.state.mode === "synthetic-local") {
    const controls = element("div", { class: "action-controls demo-controls", "aria-label": "Synthetic policy updates" });
    for (const version of ["v2", "v3"]) controls.append(button(`Import ${version}`, () => task(`Importing synthetic ${version}…`, () => mutate("/api/demo/advance", { version }, `Synthetic ${version} imported. Inspect the server’s current assessment.`)), { "data-demo-version": version }));
    $("source-metadata").append(controls);
    updateControls();
  }
  const rendered = typeof source.rendered_text === "string" ? source.rendered_text : "";
  $("source-text").textContent = rendered;
  $("source-range").hidden = true;
  if (model.highlight?.source_sha256 === source.sha256) {
    const citation = model.highlight;
    const bytes = new TextEncoder().encode(rendered);
    const decoder = new TextDecoder("utf-8", { fatal: true });
    try {
      const prefix = decoder.decode(bytes.slice(0, citation.start));
      const quote = decoder.decode(bytes.slice(citation.start, citation.end));
      const suffix = decoder.decode(bytes.slice(citation.end));
      $("source-text").replaceChildren(document.createTextNode(prefix), element("mark", { id: "active-citation", text: quote }), document.createTextNode(suffix));
      $("source-range").textContent = `${source.version_id} · UTF-8 bytes [${citation.start}, ${citation.end})`;
      $("source-range").hidden = false;
    } catch { showError(new Error("The citation does not align with UTF-8 character boundaries.")); }
  }
}

function visibleActions() {
  const search = $("decision-search").value.trim().toLowerCase();
  const filter = $("decision-filter").value;
  return model.state.actions.filter((action) => (!search || `${action.label} ${action.id} ${action.claim.result.clause}`.toLowerCase().includes(search))
    && (filter === "all" || (filter === "released" ? action.assessment.gate === "released" : ["stale", "needs_review", "source_unavailable"].includes(action.assessment.gate))));
}

function selectAction(id) {
  if (model.busy) return;
  clearPreview(); model.actionId = id; model.highlight = null;
  const action = currentAction();
  model.sourceSha = action?.claim.result.source_sha256 ?? model.sourceSha;
  renderSources(); renderRows(); renderDetail(); resetHistory(); updateControls();
  if (matchMedia("(max-width:600px)").matches) $("decision-detail").scrollIntoView({ block: "start", behavior: matchMedia("(prefers-reduced-motion:reduce)").matches ? "auto" : "smooth" });
}

function renderRows() {
  const actions = visibleActions();
  $("decision-empty").hidden = actions.length > 0;
  $("decision-empty").textContent = model.state.actions.length ? "No decisions match this view. Change the filter or search." : "No decisions are available in this workspace.";
  $("decision-rows").replaceChildren(...actions.map((action) => {
    const select = button(action.label || action.id, () => selectAction(action.id), { class: "decision-select", id: "decision-" + action.id, "aria-current": String(action.id === model.actionId) });
    select.append(element("span", { class: "decision-id", text: action.id }));
    return element("tr", { class: action.id === model.actionId ? "selected" : "" },
      element("td", {}, select), element("td", { "data-label": "Business time" }, timeNode(action.business_at)),
      element("td", { "data-label": "Support" }, badge(IMPACTS[action.assessment.impact_class])),
      element("td", { "data-label": "Outbox gate" }, badge(GATES[action.assessment.gate])),
    );
  }));
}

function showCitation(citation) {
  const source = model.state.sources.find((item) => item.sha256 === citation.source_sha256);
  try {
    if (!source || source.render_sha256 !== citation.render_sha256 || source.version_id !== citation.source_version
      || citation.unit !== "utf8-bytes" || !Number.isInteger(citation.start) || !Number.isInteger(citation.end)) throw new Error();
    const bytes = new TextEncoder().encode(source.rendered_text);
    if (citation.start < 0 || citation.end <= citation.start || citation.end > bytes.length) throw new Error();
    const decoder = new TextDecoder("utf-8", { fatal: true });
    decoder.decode(bytes.slice(0, citation.start)); decoder.decode(bytes.slice(citation.end));
    if (decoder.decode(bytes.slice(citation.start, citation.end)) !== citation.quote) throw new Error();
  } catch { showError(new Error("This citation range does not match its labelled rendered source. Inspect the stored record.")); return; }
  clearError(); model.sourceSha = source.sha256; model.highlight = citation; renderSources();
  $("source-text-details").open = true;
  $("source-text").focus({ preventScroll: true });
  $("active-citation").scrollIntoView({ block: "nearest" });
}

function citationList(citations) {
  if (!list(citations).length) return element("p", { class: "empty-state", text: "No exact citations are available." });
  return element("div", { class: "citation-list" }, citations.map((citation) => element("article", { class: "citation" },
    element("div", { class: "citation-top" }, element("code", { text: citation.pointer }), element("span", { text: `${citation.source_version} · bytes [${citation.start}, ${citation.end})` })),
    element("blockquote", {}, element("p", { text: citation.quote })),
    button(`Show in ${citation.source_version}`, () => showCitation(citation)),
  )));
}

function originalCitations(citations) {
  const items = list(citations);
  const versions = [...new Set(items.map((citation) => citation.source_version))];
  const fields = new Set(items.map((citation) => citation.pointer)).size;
  const label = ["Original citations", versions.join(", "), `${fields} ${fields === 1 ? "field" : "fields"}`].filter(Boolean).join(" · ");
  return element("details", { class: "detail-section citation-disclosure" },
    element("summary", { text: label }), citationList(items));
}

function receiptView(receipt) {
  return element("section", { class: "receipt-block" }, element("h3", { text: "Local outbox receipt" }),
    element("dl", { class: "value-grid" }, fact("Receipt", receipt.id), fact("Released at", timeNode(receipt.released_at)), fact("Action revision", receipt.action_revision), fact("Supported source", receipt.source_version)),
    rawDetails("Exact outbox payload", receipt.payload), rawDetails("Raw receipt JSON", receipt),
  );
}

function section(label, ...content) {
  return element("section", { class: "detail-section" }, element("h3", { text: label }), ...content);
}

function renderDetail() {
  const action = currentAction();
  if (!action) {
    $("detail-heading").textContent = "Select a decision"; $("detail-revision").textContent = "";
    $("detail-content").replaceChildren(element("p", { class: "empty-state", text: "Choose a decision to inspect its saved result, source support and release gate." })); return;
  }
  const result = action.claim.result, assessment = action.assessment;
  $("detail-heading").textContent = action.label || action.id;
  $("detail-revision").textContent = `Revision ${action.revision}`;
  const gate = element("div", { class: "gate-card", "data-gate": assessment.gate },
    element("div", { class: "gate-heading" }, badge(GATES[assessment.gate])),
    element("p", { class: "gate-reason", text: reasonText(assessment.gate_reason) }),
  );
  if (assessment.gate !== "released") gate.append(element("div", { class: "action-controls" },
    button("Recheck decision", () => task("Rechecking decision…", () => mutate("/api/recheck", { action_id: action.id }, "Decision rechecked. A new revision is shown.")), { id: "recheck-action" }),
    button("Preview exact release", () => task("Creating an exact release preview…", () => previewAction(action.id)), { id: "preview-action", class: "button primary" }),
  ));
  if (model.recorded) gate.append(element("p", { class: "recorded-action-note", text: "Recorded state. Run the local workflow to recheck or release." }));
  const meta = element("dl", { class: "detail-meta" }, fact("Business time · fixed", timeNode(action.business_at)), fact("Claim created", timeNode(action.claim.created_at)), fact("Original support", result.source_version), fact("Assessment selects", assessment.selected_source_version ?? "No unambiguous source"), fact("Recipe / clause", `${text(result.recipe)} / ${text(result.clause)}`), fact("Claim", action.claim.id));
  const output = section("Saved result", valueGrid(result.output), rawDetails("Saved inputs", result.inputs));
  if (result.result_status !== "supported") output.append(element("p", { class: "quiet", text: "This saved result requires review." }));
  const support = section("Source support");
  if (list(assessment.reason_paths).length) support.append(element("ul", { class: "path-list" }, assessment.reason_paths.map((path) => element("li", {}, element("code", { text: path }), element("span", { text: `Claim ${action.claim.id} → decision ${action.id}` })))));
  if (list(assessment.reason_codes).length) support.append(element("ul", { class: "reason-list" }, assessment.reason_codes.map((reason) => element("li", { text: reasonText(reason) }))));
  support.append(rawDetails("Assessment details", { assessment, source_health: model.state.health }), element("details", { class: "raw-details" }, element("summary", { text: "Recorded field reads" }), valueGrid(result.read_values)));
  $("detail-content").replaceChildren(meta, gate, output, support, originalCitations(result.citations));
  if (action.receipt) $("detail-content").append(section("Release record", receiptView(action.receipt)));
}

function resetHistory() {
  const action = currentAction();
  $("history-valid").value = action?.business_at ?? "";
  $("history-known").value = model.state?.now ?? "";
  $("history-result").replaceChildren(element("p", { class: "empty-state", text: "Select a decision and inspect a time pair." }));
  $("saved-query-label").hidden = !model.recorded;
  if (!model.recorded) return;
  const queries = list(model.recorded.as_of).filter((query) => query.action_id === model.actionId);
  $("saved-query").replaceChildren(...queries.map((query, index) => element("option", { value: index, text: `Business ${query.valid_at} · known ${query.known_at}` })));
  $("saved-query").disabled = queries.length === 0;
  if (queries.length) showRecordedQuery(0);
  else $("history-result").replaceChildren(element("p", { class: "empty-state", text: "No history queries were recorded for this decision." }));
}

function renderHistory(result) {
  if (!isObject(result) || result.read_only !== true) throw new Error("The history response was not read-only.");
  const body = element("div", {}, element("dl", { class: "value-grid" }, fact("Business time", timeNode(result.valid_at)), fact("Known at", timeNode(result.known_at)), fact("Selected source", result.selected_source_version ?? "None eligible"), fact("Result", title(result.result_status))));
  if (result.decision_exists_at_known_at === false) body.append(element("p", { class: "section-note", text: "This decision had not been recorded at that knowledge cutoff." }));
  body.append(element("h3", { text: "Historical result" }), valueGrid(result.output));
  if (list(result.reason_codes).length) body.append(element("ul", { class: "reason-list" }, result.reason_codes.map((code) => element("li", { text: reasonText(code) }))));
  body.append(element("h3", { text: "Historical citations" }), citationList(result.citations));
  if (result.receipt) body.append(section("Original release", receiptView(result.receipt)));
  $("history-result").replaceChildren(body);
}

function showRecordedQuery(index) {
  const queries = list(model.recorded?.as_of).filter((query) => query.action_id === model.actionId);
  const query = queries[index];
  if (!query) return;
  $("history-valid").value = query.valid_at; $("history-known").value = query.known_at;
  renderHistory(query.result);
}

function renderPreviewContents(preview, action) {
  const payload = preview.payload;
  const decision = action.label && action.label !== action.id ? `${action.label} · ${action.id}` : action.id;
  const metadata = element("dl", { class: "detail-meta" },
    fact("Decision", decision), fact("Revision", preview.action_revision),
    fact("Business time · UTC", element("time", { datetime: payload.business_at, text: payload.business_at })), fact("Policy support", payload.source_version));
  if (payload.citation_source_version && payload.citation_source_version !== payload.source_version) {
    metadata.append(fact("Original citation version", payload.citation_source_version));
  }
  $("preview-content").replaceChildren(metadata,
    section("Inputs", valueGrid(payload.inputs)), section("Output", valueGrid(payload.output)),
    rawDetails("Payload JSON", payload));
}

async function previewAction(actionId) {
  if (!live()) throw new Error("A release preview requires the local workspace.");
  clearPreview();
  let preview;
  try {
    preview = await post("/api/preview", { action_id: actionId });
    await reloadState();
  } catch (error) {
    try { await reloadState(); } catch { model.connected = false; }
    throw error;
  }
  const action = currentAction();
  if (!isObject(preview) || typeof preview.preview_id !== "string" || !preview.preview_id || preview.action_id !== action?.id
    || preview.action_revision !== action.revision || !isObject(preview.payload) || !Number.isFinite(preview.expires_in_seconds) || preview.expires_in_seconds <= 0) {
    throw new Error("The release preview no longer matches this decision. Request a fresh preview.");
  }
  model.preview = preview;
  renderPreviewContents(preview, action);
  $("preview-expiry").textContent = `The server expires this preview after ${preview.expires_in_seconds} seconds.`;
  $("preview-error").hidden = true; $("preview-dialog").showModal(); $("preview-heading").focus();
  announce("Review the exact payload before releasing to the local outbox.");
}

async function releasePreview() {
  const preview = model.preview;
  if (!preview) throw new Error("Request a fresh preview before release.");
  model.preview = null;
  try {
    await post("/api/release", { preview_id: preview.preview_id });
    await reloadState();
    announce("Release recorded in the local outbox. The saved receipt is shown.");
  } catch (error) {
    clearPreview();
    try { await reloadState(); } catch { model.connected = false; }
    throw error;
  }
}

function render() {
  $("workspace").hidden = false;
  $("mode-label").textContent = model.recorded ? "Recorded synthetic example" : model.state.mode === "synthetic-local" ? "Synthetic local workspace" : "Local workspace";
  renderHealth(); renderSources(); renderRows(); renderDetail(); resetHistory(); updateControls();
}

async function boot() {
  let receivedState = false;
  try {
    const state = await requestJSON("/api/state");
    receivedState = true;
    applyState(state); announce("Workspace loaded. Select a decision to inspect its support.");
  } catch (apiError) {
    if (receivedState || apiError.httpStatus === 200) {
      model.connected = false; showError(apiError); $("local-instructions").open = true;
      $("mode-label").textContent = "Invalid local state";
      $("reload-state").disabled = false; announce("The local state could not be displayed. Reload after inspecting the error.");
      $("workspace").setAttribute("aria-busy", "false"); return;
    }
    try {
      const recorded = await requestJSON("./recorded.json");
      if (!isObject(recorded) || recorded.mode !== "recorded" || !Array.isArray(recorded.stages) || !recorded.stages.length
        || !Array.isArray(recorded.as_of)) throw new Error("The recorded example is incomplete.");
      model.recorded = recorded; model.connected = false;
      for (const stage of recorded.stages) validateState(stage.state, true);
      $("recorded-controls").hidden = false;
      $("recorded-stage").replaceChildren(...recorded.stages.map((stage, index) => element("option", { value: index, text: stage.label || stage.id })));
      applyState(recorded.stages[0].state); $("local-instructions").open = true;
      announce("Recorded synthetic data loaded. Stage and history controls inspect saved results.");
    } catch {
      model.connected = false;
      $("mode-label").textContent = "Workspace unavailable";
      showError(new Error("The local API and recorded example are unavailable. Start the local workflow, then reload this page."));
      $("reload-state").disabled = false; $("local-instructions").open = true;
      announce("No workspace could be loaded.");
    }
  } finally { $("workspace").setAttribute("aria-busy", "false"); }
}

$("reload-state").addEventListener("click", () => task("Reloading state…", async () => {
  try { await reloadState(); announce("State reloaded."); }
  catch (error) { model.connected = false; clearPreview(); throw error; }
}));
$("refresh-source").addEventListener("click", () => task("Fetching the pinned source…", () => mutate("/api/refresh", {}, "Source fetch completed. Inspect availability and support separately.")));
for (const id of ["decision-search", "decision-filter"]) $(id).addEventListener(id === "decision-search" ? "input" : "change", () => {
  if (!model.state) return;
  renderRows(); updateControls();
});
$("recorded-stage").addEventListener("change", () => {
  if (!model.recorded || model.busy) return;
  const stage = model.recorded.stages[Number($("recorded-stage").value)];
  if (!stage) return;
  clearPreview(); applyState(stage.state); announce(`Recorded stage: ${stage.label || stage.id}.`);
});
$("saved-query").addEventListener("change", () => showRecordedQuery(Number($("saved-query").value)));
$("history-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const action = currentAction();
  if (!action || !live()) return;
  const query = new URLSearchParams({ action_id: action.id, known_at: $("history-known").value.trim(), valid_at: $("history-valid").value.trim() });
  task("Inspecting the read-only history query…", async () => {
    const result = await requestJSON("/api/as-of?" + query.toString()); renderHistory(result);
    announce("Historical support shown. The decision’s business time and release gate are unchanged.");
  });
});
$("close-preview").addEventListener("click", clearPreview);
$("preview-dialog").addEventListener("close", () => { model.preview = null; updateControls(); ($("preview-action") ?? $("decision-detail")).focus(); });
$("release-preview").addEventListener("click", () => task("Recording the reviewed release…", releasePreview));
boot();
