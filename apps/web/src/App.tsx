import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import {
  API_BASE,
  type Approval,
  type Comparison,
  type EventPage,
  type EventRecord,
  type EvaluationReport,
  type HarnessRecommendation,
  type HarnessResult,
  type ManagedPrompt,
  type Principal,
  type PromptCatalog,
  type Scenario,
  type Session,
  type Snapshot,
  type Topology,
  loadSession,
  request,
  saveSession,
} from "./api";

const DEV_MODE = import.meta.env.VITE_NORTHSTAR_DEV_MODE === "true";

function errorMessage(error: unknown): string { return error instanceof Error ? error.message : "The request failed."; }

function LoginPanel({ onAuthenticated }: { onAuthenticated: (session: Session, principal: Principal) => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [challenge, setChallenge] = useState<{ session: string; name: string } | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError("");
    try {
      if (challenge) {
        const result = await request<Session>("/auth/respond-to-challenge", { method: "POST", body: JSON.stringify({ username, new_password: newPassword, session: challenge.session }) }, null);
        saveSession(result); onAuthenticated(result, await request<Principal>("/auth/me", {}, result)); return;
      }
      const result = await request<Session & { status: string; challenge?: { name: string; session: string } }>("/auth/login", { method: "POST", body: JSON.stringify({ username, password }) }, null);
      if (result.status === "challenge_required" && result.challenge) {
        setChallenge({ name: result.challenge.name, session: result.challenge.session }); setPassword(""); return;
      }
      saveSession(result); onAuthenticated(result, await request<Principal>("/auth/me", {}, result));
    } catch (requestError) { setError(errorMessage(requestError)); } finally { setBusy(false); }
  }

  return <main className="login-shell">
    <section className="login-hero"><p className="eyebrow">NORTHSTAR / SYNTHETIC OPERATIONS</p><h1>See the agent investigate the whole digital twin.</h1><p className="lead">A deterministic operations console for guided investigations, current business state, and replayable activity.</p><div className="hero-proof"><span>150 devices</span><span>guided investigation</span><span>1 source of truth</span></div></section>
    <section className="panel auth-panel"><p className="eyebrow">COGNITO ACCESS</p><h2>{challenge ? "Set your permanent password" : "Sign in to the control room"}</h2><p className="muted">User creation is disabled for self-service. A Cognito administrator must invite every user.</p><form onSubmit={submit} className="stack-form"><label htmlFor="login-username">Username or email</label><input id="login-username" value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" required />{!challenge && <><label htmlFor="login-password">Password</label><input id="login-password" type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" required /></>}{challenge && <><label htmlFor="login-new-password">New password</label><input id="login-new-password" type="password" value={newPassword} onChange={(event) => setNewPassword(event.target.value)} autoComplete="new-password" required /></>}<button type="submit" disabled={busy}>{busy ? "Checking Cognito…" : challenge ? "Set password and enter" : "Enter control room"}</button>{challenge && <button type="button" className="secondary" onClick={() => setChallenge(null)}>Back to sign in</button>}{error && <p className="error-text" role="alert">{error}</p>}</form></section>
  </main>;
}

function StatusPill({ children, tone = "neutral" }: { children: string; tone?: "neutral" | "good" | "warn" | "bad" }) { return <span className={`status-pill ${tone}`}>{children}</span>; }
function CountCard({ label, value, detail }: { label: string; value: number; detail: string }) { return <div className="count-card"><span>{label}</span><strong>{value}</strong><small>{detail}</small></div>; }
function RecommendationSummary({ recommendation }: { recommendation: HarnessRecommendation }) {
  return <div className={`recommendation-summary ${recommendation.actionable ? "actionable" : "informational"}`}>
    <div className="recommendation-field recommendation-primary"><span>Proposed change</span><strong>{recommendation.proposed_change}</strong></div>
    <div className="recommendation-field"><span>Scope</span><strong>{recommendation.scope}</strong></div>
    <div className="recommendation-field"><span>Approval required</span><strong>{recommendation.approval_required}</strong></div>
    <div className="recommendation-field"><span>Execution status</span><strong>{recommendation.execution_status}</strong></div>
    <div className="recommendation-field recommendation-evidence"><span>Evidence</span><strong>{recommendation.evidence}</strong></div>
  </div>;
}

function PromptLibrary({ session }: { session: Session }) {
  const [catalog, setCatalog] = useState<ManagedPrompt[]>([]);
  const [selectedVersions, setSelectedVersions] = useState<Record<string, string>>({});
  const [versionDescriptions, setVersionDescriptions] = useState<Record<string, string>>({});
  const [preview, setPreview] = useState<{ key: string; text: string; version: string; source: string } | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const loadPrompts = useCallback(async () => {
    setError("");
    try {
      const result = await request<PromptCatalog>("/admin/prompts", {}, session);
      setCatalog(result.prompts || []);
      setSelectedVersions((previous) => Object.fromEntries((result.prompts || []).map((prompt) => [prompt.key, previous[prompt.key] || prompt.active_version || prompt.default_version || ""])));
    } catch (requestError) { setError(errorMessage(requestError)); }
  }, [session]);

  useEffect(() => { void loadPrompts(); }, [loadPrompts]);

  function updateDraft(key: string, field: keyof ManagedPrompt["draft"], value: string | number) {
    setCatalog((previous) => previous.map((prompt) => prompt.key === key ? { ...prompt, draft: { ...prompt.draft, [field]: value } } : prompt));
  }

  async function saveDraft(prompt: ManagedPrompt) {
    setBusy(`save-${prompt.key}`); setError(""); setNotice("");
    try {
      await request(`/admin/prompts/${encodeURIComponent(prompt.key)}`, {
        method: "PUT",
        body: JSON.stringify({ description: prompt.draft.description, system: prompt.draft.system, user: prompt.draft.user, model_id: prompt.draft.model_id, max_tokens: prompt.draft.max_tokens, temperature: prompt.draft.temperature }),
      }, session);
      setNotice(`${prompt.label} draft saved. Runtime still uses its active immutable version.`);
      await loadPrompts();
    } catch (requestError) { setError(errorMessage(requestError)); }
    finally { setBusy(""); }
  }

  async function createVersion(prompt: ManagedPrompt) {
    setBusy(`version-${prompt.key}`); setError(""); setNotice("");
    try {
      await request(`/admin/prompts/${encodeURIComponent(prompt.key)}/versions`, {
        method: "POST",
        body: JSON.stringify({ description: versionDescriptions[prompt.key] || "Created from the Bedrock draft." }),
      }, session);
      setVersionDescriptions((previous) => ({ ...previous, [prompt.key]: "" }));
      setNotice(`Created an immutable version for ${prompt.label}. Select it explicitly before it affects runtime.`);
      await loadPrompts();
    } catch (requestError) { setError(errorMessage(requestError)); }
    finally { setBusy(""); }
  }

  async function activateVersion(prompt: ManagedPrompt) {
    const version = selectedVersions[prompt.key] || "";
    if (!version || !/^\d+$/.test(version)) { setError("Choose an immutable numeric version before activating it."); return; }
    setBusy(`activate-${prompt.key}`); setError(""); setNotice("");
    try {
      await request(`/admin/prompts/${encodeURIComponent(prompt.key)}/active`, { method: "POST", body: JSON.stringify({ version }) }, session);
      setNotice(`${prompt.label} now uses immutable version ${version}.`);
      await loadPrompts();
    } catch (requestError) { setError(errorMessage(requestError)); }
    finally { setBusy(""); }
  }

  async function previewPrompt(prompt: ManagedPrompt) {
    const selected = selectedVersions[prompt.key];
    setBusy(`preview-${prompt.key}`); setError("");
    try {
      const result = await request<{ key: string; text: string; version: string; source: string }>(`/admin/prompts/${encodeURIComponent(prompt.key)}/preview`, {
        method: "POST",
        body: JSON.stringify({ version: selected && /^\d+$/.test(selected) ? selected : null, variables: { objective: "Confirm the business impact of the selected situation.", scenario_run_id: "preview-run-001", incident_id: "INC-0001", approval_id: "APR-preview", operation_id: "OP-preview", action: "review the approved operation", required_tools: ["get_incident", "check_service_health"], required_tools_text: "Use each of these required read-only simulator tools at least once in a sensible order: get_incident, check_service_health." } }),
      }, session);
      setPreview(result);
    } catch (requestError) { setError(errorMessage(requestError)); }
    finally { setBusy(""); }
  }

  return <section className="panel prompt-library-panel">
    <div className="section-heading"><div><p className="eyebrow">ADMIN ONLY / BEDROCK PROMPT MANAGEMENT</p><h2>Prompt library</h2></div><button className="secondary" disabled={Boolean(busy)} onClick={() => void loadPrompts()}>Refresh</button></div>
    <p className="muted">Edit a draft, create an immutable version, then explicitly select the version used by the next investigation. Draft edits never change runtime behavior by themselves.</p>
    {error && <p className="error-text" role="alert">{error}</p>}
    {notice && <p className="success-text" role="status">{notice}</p>}
    {!catalog.length && !error ? <p className="empty-state">Loading managed prompts…</p> : <div className="prompt-list">{catalog.map((prompt) => {
      const selected = selectedVersions[prompt.key] || prompt.active_version || prompt.default_version || "";
      return <article className="prompt-card" key={prompt.key}>
        <div className="section-heading"><div><p className="eyebrow">{prompt.key}</p><h3 className="prompt-title">{prompt.label}</h3><code>{prompt.arn || "Bedrock ARN unavailable"}</code></div><StatusPill tone={prompt.active_version && prompt.active_version !== "DRAFT" ? "good" : "warn"}>{prompt.active_version ? `v${prompt.active_version} active` : "No active version"}</StatusPill></div>
        <p className="muted">{prompt.description}</p>
        <div className="prompt-meta"><span>DRAFT · {prompt.draft.updated_at ? new Date(prompt.draft.updated_at).toLocaleString() : "seeded"}</span><span>Variables · {prompt.draft.input_variables.map((name) => `{{${name}}}`).join(", ") || "none"}</span></div>
        <div className="prompt-editor">
          <label>Description<input value={prompt.draft.description} onChange={(event) => updateDraft(prompt.key, "description", event.target.value)} /></label>
          <label>System instructions<textarea rows={4} value={prompt.draft.system} onChange={(event) => updateDraft(prompt.key, "system", event.target.value)} /></label>
          <label>User message template<textarea rows={8} value={prompt.draft.user} onChange={(event) => updateDraft(prompt.key, "user", event.target.value)} /></label>
          <div className="prompt-parameter-grid"><label>Model ID<input value={prompt.draft.model_id} onChange={(event) => updateDraft(prompt.key, "model_id", event.target.value)} /></label><label>Max tokens<input type="number" min="1" value={prompt.draft.max_tokens} onChange={(event) => updateDraft(prompt.key, "max_tokens", Number(event.target.value))} /></label><label>Temperature<input type="number" min="0" max="2" step="0.1" value={prompt.draft.temperature} onChange={(event) => updateDraft(prompt.key, "temperature", Number(event.target.value))} /></label></div>
          <div className="prompt-actions"><button disabled={Boolean(busy)} onClick={() => void saveDraft(prompt)}>{busy === `save-${prompt.key}` ? "Saving…" : "Save draft"}</button><button className="secondary" disabled={Boolean(busy)} onClick={() => void previewPrompt(prompt)}>{busy === `preview-${prompt.key}` ? "Rendering…" : "Preview"}</button></div>
        </div>
        <div className="prompt-runtime-controls"><label>Runtime version<select value={selected} onChange={(event) => setSelectedVersions((previous) => ({ ...previous, [prompt.key]: event.target.value }))}><option value="">Choose a version</option>{prompt.versions.map((version) => <option key={version.version} value={version.version}>v{version.version}{version.version === prompt.active_version ? " · active" : ""}{version.version === prompt.default_version ? " · initial" : ""}</option>)}</select></label><button disabled={Boolean(busy) || !selected || selected === prompt.active_version} onClick={() => void activateVersion(prompt)}>{busy === `activate-${prompt.key}` ? "Activating…" : "Use selected version"}</button></div>
        <div className="prompt-version-create"><label>Version description<input value={versionDescriptions[prompt.key] || ""} placeholder="What changed?" onChange={(event) => setVersionDescriptions((previous) => ({ ...previous, [prompt.key]: event.target.value }))} /></label><button className="secondary" disabled={Boolean(busy)} onClick={() => void createVersion(prompt)}>{busy === `version-${prompt.key}` ? "Creating…" : "Create immutable version"}</button></div>
        <div className="prompt-version-list"><strong>Version history</strong>{prompt.versions.length ? prompt.versions.map((version) => <div className={`prompt-version-row ${version.version === prompt.active_version ? "active" : ""}`} key={version.version}><span><strong>v{version.version}</strong> {version.description || "No description"}</span><small>{version.version === prompt.active_version ? "ACTIVE · " : ""}{version.created_at ? new Date(version.created_at).toLocaleString() : "date unavailable"}</small></div>) : <span className="muted">No immutable versions returned.</span>}</div>
      </article>;
    })}</div>}
    {preview && <div className="prompt-preview"><div className="section-heading"><div><p className="eyebrow">RENDERED PREVIEW</p><h3>{preview.key} · v{preview.version}</h3></div><StatusPill tone="neutral">{preview.source}</StatusPill></div><pre className="result-box">{preview.text}</pre></div>}
  </section>;
}

type ChangePlan = { action: string; label: string; targetIds: string[]; payload: Record<string, unknown>; scope: string; scopeTokens: string[]; idempotencyPrefix: string };

type RecommendationField = "proposed_change" | "scope" | "approval_required" | "evidence" | "execution_status";

function cleanRecommendationValue(value: string): string {
  return value.replaceAll("**", "").replaceAll("`", "").replace(/\s+/g, " ").trim();
}

function recommendationHasNoChange(value: string): boolean {
  const normalized = value.toLowerCase();
  return !normalized || /\b(?:none|no (?:safe )?change|no remediation|not proposed|not recommended|further investigation required before remediation)\b/.test(normalized);
}

function parseRecommendation(report: string): HarnessRecommendation {
  const fields: Partial<Record<RecommendationField, string>> = {};
  const pattern = /^\s*(?:[-*]\s+)?(?:#{1,6}\s*)?(?:\*\*)?(Proposed change|Scope|Approval required|Evidence|Execution status)(?:\*\*)?\s*:\s*(.*?)\s*$/i;
  let current: RecommendationField | null = null;
  for (const line of report.split(/\r?\n/)) {
    const match = line.match(pattern);
    if (match) {
      current = match[1].toLowerCase().replaceAll(" ", "_") as RecommendationField;
      fields[current] = cleanRecommendationValue(match[2]);
    } else if (current && line.trim()) {
      fields[current] = `${fields[current] || ""} ${cleanRecommendationValue(line)}`.trim();
    }
  }
  const parsedProposedChange = fields.proposed_change || "";
  const noChangeReported = /\b(?:no remediation (?:(?:has been )?(?:proposed|recommended)|attempted)|no change is recommended|no state-changing action)\b/i.test(report);
  const actionable = Boolean(parsedProposedChange) && !recommendationHasNoChange(parsedProposedChange);
  const proposedChange = parsedProposedChange || (noChangeReported ? "No change recommended by the agent." : "No explicit change recommendation was provided.");
  const approvalRequired = fields.approval_required || (actionable ? "Not explicitly stated by the agent." : "No — the agent did not recommend a state-changing action.");
  return {
    proposed_change: proposedChange,
    scope: fields.scope || "Not specified by the agent.",
    approval_required: approvalRequired,
    approval_required_yes: /^(?:yes|true)\b/i.test(approvalRequired),
    evidence: fields.evidence || "See the full investigation report for the supporting observations.",
    execution_status: fields.execution_status || "Not executed (read-only investigation).",
    actionable,
    structured: Object.keys(fields).length > 0,
  };
}

function recommendationMatchesChange(plan: ChangePlan | null, recommendation: HarnessRecommendation | null): boolean {
  if (!plan || !recommendation?.actionable) return false;
  const proposed = recommendation.proposed_change.toLowerCase();
  const scope = recommendation.scope.toLowerCase();
  const scopeMatches = plan.scopeTokens.every((token) => scope.includes(token.toLowerCase()));
  if (plan.action === "restart_service") return scopeMatches && /\b(?:restart|restore|recover)\b/.test(proposed);
  if (plan.action === "rollback_vpn_deployment") return scopeMatches && /\b(?:rollback|restore|recover)\b/.test(proposed);
  return false;
}

function Dashboard({ session, principal, onSignOut }: { session: Session; principal: Principal; onSignOut: () => void }) {
  const [health, setHealth] = useState("Checking API…");
  const [scenarios, setScenarios] = useState<Scenario[]>([]);
  const [scenarioId, setScenarioId] = useState("");
  const [objective, setObjective] = useState("Understand the selected business situation, identify its impact, and recommend the safest next step.");
  const [runId, setRunId] = useState<string | null>(null);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [topology, setTopology] = useState<Topology | null>(null);
  const [comparison, setComparison] = useState<Comparison | null>(null);
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [events, setEvents] = useState<EventRecord[]>([]);
  const [cursor, setCursor] = useState(0);
  const cursorRef = useRef(0);
  const [selectedEntity, setSelectedEntity] = useState<{ type: string; id: string; data: unknown } | null>(null);
  const [harness, setHarness] = useState<HarnessResult | null>(null);
  const [harnessSessionId, setHarnessSessionId] = useState<string | null>(null);
  const [evaluation, setEvaluation] = useState<EvaluationReport | null>(null);
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState("");
  const [newUserEmail, setNewUserEmail] = useState("");
  const [adminNotice, setAdminNotice] = useState("");

  function jumpToEvidence(assertion: EvaluationReport["assertions"][number]) {
    const sequences = assertion.evidence.event_sequences;
    if (!Array.isArray(sequences) || typeof sequences[0] !== "number") return;
    document.getElementById(`event-${sequences[0]}`)?.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  const loadRun = useCallback(async (selectedRunId: string, includeEvents = true) => {
    const [nextSnapshot, nextTopology, nextComparison, nextApprovals] = await Promise.all([
      request<Snapshot>(`/simulator/runs/${selectedRunId}`, {}, session),
      request<Topology>(`/simulator/runs/${selectedRunId}/topology`, {}, session),
      request<Comparison>(`/simulator/runs/${selectedRunId}/compare`, {}, session),
      request<Approval[]>(`/simulator/runs/${selectedRunId}/approvals`, {}, session),
    ]);
    setSnapshot(nextSnapshot); setTopology(nextTopology); setComparison(nextComparison); setApprovals(nextApprovals);
    if (includeEvents) {
      const page = await request<EventPage>(`/simulator/runs/${selectedRunId}/events?after_sequence=0&limit=500`, {}, session);
      setEvents(page.events); cursorRef.current = page.next_sequence; setCursor(page.next_sequence);
    }
  }, [session]);

  useEffect(() => {
    let disposed = false;
    Promise.all([request<{ phase: string }>("/healthz", {}, null), request<Scenario[]>("/simulator/scenarios", {}, session)]).then(([apiHealth, catalog]) => {
      if (disposed) return; setHealth(`API online · phase ${apiHealth.phase}`); setScenarios(catalog); setScenarioId(catalog[0]?.scenario_id || "");
    }).catch((error) => { if (!disposed) setHealth(`API unavailable · ${errorMessage(error)}`); });
    return () => { disposed = true; };
  }, [session]);

  useEffect(() => {
    if (!runId) return;
    let disposed = false;
    const refresh = async () => {
      try {
        const page = await request<EventPage>(`/simulator/runs/${runId}/events?after_sequence=${cursorRef.current}&limit=500`, {}, session);
        if (disposed || page.events.length === 0) return;
        const nextEvents = page.events.filter((event) => event.sequence > cursorRef.current);
        setEvents((previous) => [...previous, ...nextEvents]); cursorRef.current = page.next_sequence; setCursor(page.next_sequence);
        await loadRun(runId, false);
      } catch (error) { if (!disposed) setNotice(errorMessage(error)); }
    };
    const interval = window.setInterval(refresh, 1200);
    return () => { disposed = true; window.clearInterval(interval); };
  }, [loadRun, runId, session]);

  const selectedScenario = useMemo(() => scenarios.find((scenario) => scenario.scenario_id === (snapshot?.scenario_id || scenarioId)), [scenarioId, scenarios, snapshot?.scenario_id]);
  const openIncidents = useMemo(() => Object.values(snapshot?.state.incidents || {}).filter((incident) => incident.status === "open").slice(0, 8), [snapshot]);
  const devices = useMemo(() => Object.values(snapshot?.state.devices || {}).slice(0, 60), [snapshot]);
  const rollbackTargets = useMemo(() => {
    const incident = openIncidents.find((item) => item.incident_id === "INC-0001") || openIncidents[0];
    return incident?.affected_device_ids || Object.values(snapshot?.state.devices || {})
      .filter((device) => device.vpn_status === "disconnected")
      .map((device) => device.device_id);
  }, [openIncidents, snapshot]);
  const changePlan = useMemo<ChangePlan | null>(() => {
    const restrictedActions = selectedScenario?.restricted_actions || [];
    if (restrictedActions.includes("restart_service")) {
      return {
        action: "restart_service",
        label: "Restore the affected business service",
        targetIds: ["svc-payments"],
        payload: {},
        scope: "svc-payments / INC-0003 / Payments Platform",
        scopeTokens: ["svc-payments", "inc-0003"],
        idempotencyPrefix: "restart-service",
      };
    }
    if (restrictedActions.includes("rollback_deployment") && rollbackTargets.length > 0) {
      return {
        action: "rollback_vpn_deployment",
        label: "Restore workforce connectivity",
        targetIds: rollbackTargets,
        payload: { deployment_id: "DEP-0001" },
        scope: `DEP-0001 / INC-0001 / all ${rollbackTargets.length} affected device(s)`,
        scopeTokens: ["dep-0001", "inc-0001", String(rollbackTargets.length)],
        idempotencyPrefix: "restore-connectivity",
      };
    }
    return null;
  }, [rollbackTargets, selectedScenario, snapshot]);
  const changedCount = useMemo(() => Object.values(comparison?.changed || {}).reduce((total, items) => total + items.length, 0), [comparison]);
  const agentTurnCount = useMemo(() => events.filter((event) => event.event_type === "agent_turn_started").length, [events]);
  const agentToolRequestCount = useMemo(() => events.filter((event) => event.event_type === "agent_tool_call_requested").length, [events]);
  const toolObservationCount = useMemo(() => events.filter((event) => event.event_type === "observation_recorded").length, [events]);
  const agentReport = harness && harness.status !== "failed" && harness.status !== "resume_failed" && harness.succeeded !== false ? harness.text?.trim() || "" : "";
  const agentRecommendation = useMemo(() => {
    if (!agentReport) return null;
    return harness?.recommendation || parseRecommendation(agentReport);
  }, [agentReport, harness?.recommendation]);
  const approvalMatchesRecommendation = recommendationMatchesChange(changePlan, agentRecommendation);

  async function createRun(event: FormEvent) {
    event.preventDefault(); if (!scenarioId) return; setBusy("create"); setNotice("");
    try { const result = await request<Snapshot>("/simulator/runs", { method: "POST", body: JSON.stringify({ scenario_id: scenarioId }) }, session); setRunId(result.run_id); setHarness(null); setHarnessSessionId(null); setEvaluation(null); cursorRef.current = 0; setCursor(0); setEvents([]); await loadRun(result.run_id); }
    catch (error) { setNotice(errorMessage(error)); } finally { setBusy(""); }
  }

  async function resetRun() {
    if (!runId) return; setBusy("reset");
    try { await request(`/simulator/runs/${runId}/reset`, { method: "POST", body: "{}" }, session); setHarness(null); setHarnessSessionId(null); setEvaluation(null); await loadRun(runId); setNotice("Run reset to its deterministic baseline."); }
    catch (error) { setNotice(errorMessage(error)); } finally { setBusy(""); }
  }

  async function openEntity(type: string, id: string) {
    if (!runId) return;
    try { setSelectedEntity({ type, id, data: await request(`/simulator/runs/${runId}/entities/${type}/${id}`, {}, session) }); }
    catch (error) { setNotice(errorMessage(error)); }
  }

  async function invokeHarness() {
    if (!runId) return; setBusy("harness"); setHarness({ status: "invoking" });
    try { const result = await request<HarnessResult>("/harness/invocations", { method: "POST", body: JSON.stringify({ scenario_run_id: runId, objective }) }, session); setHarness(result); if (typeof result.session_id === "string") setHarnessSessionId(result.session_id); await loadRun(runId); }
    catch (error) { setHarness({ status: "failed", text: errorMessage(error) }); } finally { setBusy(""); }
  }

  async function resumeHarnessAfterApproval(approval: Approval) {
    if (!runId || !harnessSessionId) {
      setNotice("Operation completed. Start the investigation first to establish a resumable session.");
      return;
    }
    setBusy("resume"); setHarness({ status: "resuming", session_id: harnessSessionId });
    try {
      const result = await request<HarnessResult>("/harness/invocations", {
        method: "POST",
        body: JSON.stringify({
          scenario_run_id: runId,
          objective,
          session_id: harnessSessionId,
          approval_id: approval.approval_id,
          operation_id: approval.operation_id,
          action: approval.action,
        }),
      }, session);
      setHarness(result);
      if (typeof result.session_id === "string") setHarnessSessionId(result.session_id);
      await loadRun(runId);
    } catch (error) { setHarness({ status: "resume_failed", text: errorMessage(error), session_id: harnessSessionId }); }
    finally { setBusy(""); }
  }

  async function createUser(event: FormEvent) {
    event.preventDefault(); setAdminNotice("");
    try { const result = await request<{ status: string; email: string }>("/admin/users", { method: "POST", body: JSON.stringify({ email: newUserEmail }) }, session); setAdminNotice(`${result.status}: invitation sent to ${result.email}.`); setNewUserEmail(""); }
    catch (error) { setAdminNotice(errorMessage(error)); }
  }

  async function proposeChange() {
    if (!runId || !changePlan) return; setBusy("proposal"); setNotice("");
    try {
      await request(`/simulator/runs/${runId}/operations/proposals`, { method: "POST", body: JSON.stringify({ action: changePlan.action, target_ids: changePlan.targetIds, payload: { ...changePlan.payload, recommendation: { proposed_change: agentRecommendation?.proposed_change, scope: agentRecommendation?.scope, approval_required: agentRecommendation?.approval_required } }, idempotency_key: `${changePlan.idempotencyPrefix}-${runId}-${changePlan.targetIds.join("-")}` }) }, session);
      await loadRun(runId, false); setNotice(`${changePlan.label} request submitted for approval.`);
    } catch (error) { setNotice(errorMessage(error)); } finally { setBusy(""); }
  }

  async function decideApproval(approvalId: string, decision: "approved" | "rejected") {
    if (!runId) return; setBusy(`decision-${approvalId}`);
    try { await request(`/simulator/runs/${runId}/approvals/${approvalId}/decision`, { method: "POST", body: JSON.stringify({ decision }) }, session); await loadRun(runId, false); }
    catch (error) { setNotice(errorMessage(error)); } finally { setBusy(""); }
  }

  async function executeApproval(approvalId: string) {
    if (!runId) return; setBusy(`execute-${approvalId}`);
    try {
      const result = await request<{ approval: Approval }>(`/simulator/runs/${runId}/approvals/${approvalId}/execute`, { method: "POST", body: "{}" }, session);
      await loadRun(runId);
      if (result.approval.status === "verified" || result.approval.status === "executed") await resumeHarnessAfterApproval(result.approval);
    }
    catch (error) { setNotice(errorMessage(error)); } finally { setBusy(""); }
  }

  async function evaluateCurrentRun() {
    if (!runId) return; setBusy("evaluation");
    try { setEvaluation(await request<EvaluationReport>(`/evaluation/runs/${runId}`, {}, session)); }
    catch (error) { setNotice(errorMessage(error)); }
    finally { setBusy(""); }
  }

  return <div className="app-shell">
    <header className="topbar"><div><p className="eyebrow">NORTHSTAR / SYNTHETIC OPERATIONS</p><h1>Agent operations console</h1><p className="muted">Agent recommendations, current business state, and replayable activity.</p></div><div className="topbar-right"><StatusPill tone="good">{health}</StatusPill><span className="user-chip">{principal.email || principal.username}{principal.is_admin ? " · admin" : ""}</span><button className="secondary" onClick={() => { saveSession(null); onSignOut(); }}>Sign out</button></div></header>
    <main className="content-shell">
      <section className="panel scenario-panel"><div className="section-heading"><div><p className="eyebrow">SCENARIO CATALOG</p><h2>Choose a business situation</h2></div><span className="muted">{runId ? "Investigation in progress" : "No investigation selected"}</span></div><form className="controls" onSubmit={createRun}><label>Business situation<select value={scenarioId} onChange={(event) => setScenarioId(event.target.value)}>{scenarios.map((scenario) => <option key={scenario.scenario_id} value={scenario.scenario_id}>{scenario.name}</option>)}</select></label><label className="wide-field">Investigation goal<input value={objective} onChange={(event) => setObjective(event.target.value)} /></label><button type="submit" disabled={!scenarioId || Boolean(busy)}>{busy === "create" ? "Creating…" : "Create investigation"}</button><button type="button" className="secondary" disabled={!runId || Boolean(busy)} onClick={resetRun}>{busy === "reset" ? "Resetting…" : "Reset"}</button></form>{selectedScenario && <div className="scenario-description" aria-live="polite"><div><p className="eyebrow">WHAT THIS SITUATION SIMULATES</p><h3>{selectedScenario.name}</h3></div><p>{selectedScenario.description}</p><small><strong>Investigation goal:</strong> {selectedScenario.objective}</small></div>}{notice && <p className="error-text">{notice}</p>}</section>

      {principal.is_admin && <section className="panel admin-panel"><div><p className="eyebrow">ADMIN ONLY</p><h2>Invite a Cognito user</h2><p className="muted">The backend checks the <code>northstar-admin</code> group. User-pool self sign-up is disabled.</p></div><form className="admin-form" onSubmit={createUser}><label htmlFor="admin-email">Operator email</label><input id="admin-email" type="email" placeholder="operator@example.com" value={newUserEmail} onChange={(event) => setNewUserEmail(event.target.value)} required /><button type="submit">Invite user</button></form>{adminNotice && <p className={adminNotice.startsWith("INVITED") ? "success-text" : "error-text"} role="status">{adminNotice}</p>}</section>}

      {principal.is_admin && <PromptLibrary session={session} />}

      <section className="panel approval-panel">
        <div className="section-heading"><div><p className="eyebrow">HUMAN APPROVAL</p><h2>What change does the agent recommend?</h2></div><StatusPill tone={approvals.some((approval) => approval.status === "requested") ? "warn" : "neutral"}>{approvals.length ? `${approvals.length} request(s)` : "No requests"}</StatusPill></div>
        <p className="muted">The operations agent investigates first. This panel shows the proposed change, scope, evidence, and approval decision. Nothing is submitted or executed until the recommendation is explicit.</p>
        {agentRecommendation ? <div className="agent-recommendation"><p className="eyebrow">AGENT RECOMMENDATION</p><RecommendationSummary recommendation={agentRecommendation} />{agentReport && <details className="report-details"><summary>Show full agent report</summary><pre className="recommendation-box">{agentReport}</pre></details>}</div> : <p className="empty-state">Start the investigation below. The agent’s proposed change will appear here before any approval request can be created.</p>}
        {changePlan && agentRecommendation && approvalMatchesRecommendation && agentRecommendation.approval_required_yes ? <div className="approval-toolbar"><span className="target-summary"><strong>Change ready to request approval</strong><small>Agent proposed: {agentRecommendation.proposed_change}</small><small>Agent scope: {agentRecommendation.scope}</small><small>Application targets: {changePlan.label} · {changePlan.scope}</small><small>Approval required: {agentRecommendation.approval_required}</small></span><button disabled={!runId || Boolean(busy)} onClick={proposeChange}>{busy === "proposal" ? "Requesting…" : "Submit for approval"}</button></div> : changePlan && agentRecommendation && agentRecommendation.actionable && !agentRecommendation.approval_required_yes ? <p className="empty-state">The agent proposed “{agentRecommendation.proposed_change}”, but did not say that approval is required. No approval request was created.</p> : changePlan && agentRecommendation && agentRecommendation.actionable ? <p className="error-text">The agent proposed “{agentRecommendation.proposed_change}”, but it does not match an executable action allowed for this situation. No approval request was created.</p> : changePlan && agentRecommendation ? <p className="empty-state">No state-changing change is recommended for approval. The agent’s evidence does not support a safe action yet.</p> : changePlan ? <p className="empty-state">After the investigation completes, the agent’s recommendation and exact scope will appear here for approval.</p> : agentRecommendation && agentRecommendation.actionable ? <p className="error-text">The agent proposed a change, but this situation has no executable approval action in the console.</p> : agentReport ? <p className="empty-state">No state-changing change is recommended for approval.</p> : <p className="empty-state">No recommendation is available yet.</p>}
        {approvals.length ? <div className="approval-list">{approvals.slice().reverse().map((approval) => <div className="approval-row" key={approval.approval_id}><div><strong>{approvalActionLabel(approval.action)}</strong><small>Scope: {approval.target_ids.length} target(s) · {approval.target_ids.join(", ")}</small><small>Requested by {approval.requested_by} · expires {new Date(approval.expires_at).toLocaleString()}</small></div><div className="approval-actions"><StatusPill tone={approvalTone(approval.status)}>{approval.status}</StatusPill>{principal.is_admin && approval.status === "requested" && <><button className="secondary" disabled={Boolean(busy)} onClick={() => decideApproval(approval.approval_id, "rejected")}>Reject</button><button disabled={Boolean(busy)} onClick={() => decideApproval(approval.approval_id, "approved")}>Approve</button></>}{(approval.status === "approved" || approval.status === "executed") && <button disabled={Boolean(busy)} onClick={() => executeApproval(approval.approval_id)}>{approval.status === "executed" ? "Retry & verify" : "Execute & verify"}</button>}</div></div>)}</div> : agentRecommendation && changePlan && approvalMatchesRecommendation ? <p className="empty-state">No approval request yet.</p> : null}
      </section>

      <section className="dashboard-grid"><article className="panel summary-panel"><div className="section-heading"><div><p className="eyebrow">AUTHORITATIVE SNAPSHOT</p><h2>Environment</h2></div><StatusPill tone={snapshot ? "good" : "neutral"}>{snapshot ? `revision ${snapshot.summary.revision}` : "Create a run"}</StatusPill></div>{snapshot ? <><div className="count-grid"><CountCard label="Devices" value={snapshot.summary.entity_counts.devices || 0} detail={formatCounts(snapshot.summary.device_status_counts)} /><CountCard label="Connectivity" value={snapshot.summary.entity_counts.devices || 0} detail={formatCounts(snapshot.summary.vpn_status_counts)} /><CountCard label="Services" value={snapshot.summary.entity_counts.services || 0} detail={formatCounts(snapshot.summary.service_status_counts)} /><CountCard label="Incidents" value={snapshot.summary.entity_counts.incidents || 0} detail={formatCounts(snapshot.summary.incident_status_counts)} /></div><div className="comparison"><strong>Before / after</strong><span>{changedCount ? `${changedCount} entity changes · revision ${comparison?.before.revision} → ${comparison?.after.revision}` : "No authoritative state changes yet."}</span></div><h3>Open incidents</h3><div className="entity-list">{openIncidents.length ? openIncidents.map((incident) => <button className="entity-row" key={incident.incident_id} onClick={() => openEntity("incident", incident.incident_id)}><span><strong>{incident.incident_id}</strong> {incident.title}</span><StatusPill tone="warn">{incident.severity}</StatusPill></button>) : <p className="muted">No open incidents.</p>}</div></> : <p className="empty-state">Create a run to load the digital twin.</p>}</article>
        <article className="panel topology-panel"><div className="section-heading"><div><p className="eyebrow">SERVICE GRAPH</p><h2>Dependencies</h2></div></div>{topology ? <div className="service-list">{topology.services.map((service) => <div className="service-node" key={service.service_id}><div><strong>{service.name}</strong><span className="muted">{service.service_id}</span></div><StatusPill tone={service.status === "operational" ? "good" : "bad"}>{service.status}</StatusPill>{service.dependency_ids.length > 0 && <small>depends on {service.dependency_ids.join(", ")}</small>}</div>)}</div> : <p className="empty-state">Create a run to load service topology.</p>}</article></section>

      <section className="dashboard-grid lower-grid"><article className="panel device-panel"><div className="section-heading"><div><p className="eyebrow">FLEET VIEW</p><h2>Devices</h2></div><span className="muted">{devices.length ? `${snapshot?.summary.entity_counts.devices} total · showing ${devices.length}` : ""}</span></div>{devices.length ? <div className="table-wrap"><table><caption className="sr-only">Synthetic device fleet</caption><thead><tr><th>Device</th><th>Office</th><th>Health</th><th>Connectivity</th></tr></thead><tbody>{devices.map((device) => <tr key={device.device_id} onClick={() => openEntity("device", device.device_id)}><td><button className="link-button" onClick={(event) => { event.stopPropagation(); openEntity("device", device.device_id); }}>{device.device_id}</button><small>{device.hostname}</small></td><td>{device.office}</td><td><StatusPill tone={device.status === "healthy" ? "good" : "warn"}>{device.status}</StatusPill></td><td><StatusPill tone={device.vpn_status === "connected" ? "good" : "bad"}>{device.vpn_status}</StatusPill></td></tr>)}</tbody></table></div> : <p className="empty-state">No run devices to display.</p>}</article><article className="panel agent-panel"><div className="section-heading"><div><p className="eyebrow">AGENT INVESTIGATION</p><h2>Investigation activity</h2></div><button disabled={!runId || Boolean(busy)} onClick={invokeHarness}>{busy === "harness" ? "Starting…" : "Start investigation"}</button></div><p className="muted">The operations agent reviews the selected situation, checks the current business state, and explains its recommendation. After an approved change, it checks the result again.</p><p className="hint">{agentTurnCount} reasoning step(s) · {agentToolRequestCount} system check(s) · {toolObservationCount} evidence item(s)</p>{harness ? <pre className="result-box">{harness.text || JSON.stringify(harness, null, 2)}</pre> : <p className="empty-state">Start the investigation after creating a situation.</p>}{harnessSessionId && <p className="hint">This investigation can continue after an approved change.</p>}</article></section>

      <section className="panel evaluation-panel"><div className="section-heading"><div><p className="eyebrow">INDEPENDENT EVALUATION</p><h2>Score the authoritative run</h2></div><button className="secondary" disabled={!runId || Boolean(busy)} onClick={evaluateCurrentRun}>{busy === "evaluation" ? "Scoring…" : "Evaluate run"}</button></div><p className="muted">The evaluator reads simulator state, durable events, and typed observations. It does not trust the agent’s final prose.</p>{evaluation ? <><div className="evaluation-summary"><StatusPill tone={evaluation.status === "passed" ? "good" : "bad"}>{`${evaluation.status} · ${evaluation.score}%`}</StatusPill><span>{evaluation.metrics.tool_call_count || 0} observations · {evaluation.metrics.mutation_event_count || 0} mutation events · {evaluation.metrics.recovery_event_count || 0} recovery events</span></div><div className="evaluation-failures">{evaluation.assertions.filter((assertion) => !assertion.passed).slice(0, 4).map((assertion) => <button className="evidence-link" key={assertion.name} onClick={() => jumpToEvidence(assertion)}>{assertion.name}</button>)}</div></> : <p className="empty-state">Evaluate after collecting evidence or completing a remediation.</p>}</section>

      <section className="panel timeline-panel"><div className="section-heading"><div><p className="eyebrow">REPLAYABLE EVENT LOG</p><h2>Execution timeline</h2></div><span className="muted">{runId ? `sequence ${cursor}` : "No run selected"}</span></div>{events.length ? <div className="timeline">{events.slice(-80).map((event) => <div className="timeline-item" id={`event-${event.sequence}`} key={`${event.scenario_run_id}-${event.sequence}`}><span className="sequence">{event.sequence}</span><div><strong>{event.event_type}</strong><small>{event.occurred_at} · revision {event.state_revision}</small><p>{eventDetail(event)}</p></div></div>)}</div> : <p className="empty-state">Events will appear here and replay from the authoritative sequence.</p>}</section>
    </main>
    {selectedEntity && <div className="drawer-backdrop" onClick={() => setSelectedEntity(null)}><aside className="drawer" role="dialog" aria-modal="true" aria-label="Entity detail" onClick={(event) => event.stopPropagation()}><button className="close-button" aria-label="Close entity detail" onClick={() => setSelectedEntity(null)}>×</button><p className="eyebrow">ENTITY DETAIL</p><h2>{selectedEntity.type}: {selectedEntity.id}</h2><pre className="result-box">{JSON.stringify(selectedEntity.data, null, 2)}</pre></aside></div>}
  </div>;
}

function formatCounts(counts: Record<string, number> | undefined): string { return Object.entries(counts || {}).map(([key, value]) => `${value} ${key}`).join(" · ") || "—"; }
function eventDetail(event: EventRecord): string {
  const payload = event.payload || {};
  if (event.event_type === "agent_tool_call_requested") return `${String(payload.tool_name || "tool")} · ${String(payload.tool_use_id || "request")}`;
  if (event.event_type === "agent_turn_started") return `turn ${String(payload.turn_index || "?")} · request ${String(payload.request_id || "")}`;
  if (event.event_type === "agent_turn_completed") return `stop reason: ${String(payload.stop_reason || "unknown")}`;
  if (event.event_type === "agent_invocation_started") return `session ${String(payload.session_id || "")}`;
  if (event.event_type === "agent_invocation_completed") return `${String(payload.turn_count || 0)} turn(s) · ${String(payload.tool_call_count || 0)} tool call(s) · ${String(payload.stop_reason || "")}`;
  if (event.event_type === "agent_error") return `${String(payload.error_code || "error")}: ${String(payload.message || "")}`;
  return String(payload.tool_name || payload.fault_type || payload.seed || payload.status || payload.request_id || "");
}
function approvalTone(status: string): "neutral" | "good" | "warn" | "bad" { return status === "verified" ? "good" : status === "requested" || status === "executed" ? "warn" : status === "rejected" || status === "expired" ? "bad" : "neutral"; }
function approvalActionLabel(action: string): string {
  if (action === "rollback_vpn_deployment") return "Restore workforce connectivity";
  if (action === "restart_service") return "Restore the affected business service";
  if (action === "update_incident") return "Update the incident status";
  return action.replaceAll("_", " ");
}

export default function App() {
  const [session, setSession] = useState<Session | null>(loadSession());
  const [principal, setPrincipal] = useState<Principal | null>(null);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    if (DEV_MODE) { setPrincipal({ sub: "local", username: "local-operator", email: "local@example.com", groups: [], is_admin: false }); setLoading(false); return; }
    const stored = loadSession();
    if (!stored) { setLoading(false); return; }
    request<Principal>("/auth/me", {}, stored).then((current) => { setSession(stored); setPrincipal(current); }).catch(() => { saveSession(null); setSession(null); setPrincipal(null); }).finally(() => setLoading(false));
  }, []);
  if (loading) return <div className="loading-screen">Connecting to {API_BASE}…</div>;
  if (!principal || (!session && !DEV_MODE)) return <LoginPanel onAuthenticated={(nextSession, nextPrincipal) => { setSession(nextSession); setPrincipal(nextPrincipal); }} />;
  return <Dashboard session={session || { access_token: "", id_token: "" }} principal={principal} onSignOut={() => { setSession(null); setPrincipal(null); }} />;
}
