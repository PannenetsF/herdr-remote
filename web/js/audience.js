// ---- Human audience: Projects shows only coordinators and hand-started sessions -------------
//
// Keep the raw `agents` array intact: All panes, the terminal and sibling navigation still address
// every live pane. Projects is a strictly primary-session surface: an hp_role=thread worker is
// NEVER rendered as its own card, no matter its hp_audience or process status. Human/stalled
// worker escalations are aggregated onto the owning coordinator card in the same project+host.
// Authoritative hp_* tokens win over names and directories; older hosts fail open so a plain
// session never disappears.
const HP_THREAD_NAME = /^hp-.+-t-\d+/;
const HP_THREAD_CWD = /\/threads\/t-\d+\/?$/;
const HP_WORKTREE_CWD = /\/worktrees\/hp-.+-t-\d+(?:[-/]|$)/;

function primarySession(p) {
  const role = (p.hp_role || '').trim();
  if (role) return role === 'coordinator' || role === 'session';
  const name = (p.label || '').trim();
  if (name.startsWith('hpc-')) return true;
  if (HP_THREAD_NAME.test(name)) return false;
  if (HP_THREAD_CWD.test(p.cwd || '') || HP_WORKTREE_CWD.test(p.cwd || '')) return false;
  return !/^t-\d+$/.test((p.project || '').trim());
}

// An authoritative worker thread. guest/other are not threads but are also never primary, so they
// stay out of Projects through primarySession() returning false.
function isThread(p) {
  return (p.hp_role || '').trim() === 'thread';
}

// A thread its project has escalated to a human: `human` (reports/failures) or `stalled` (an
// internal question that outlived the coordinator window). Internal threads never count.
function escalatedThread(p) {
  return isThread(p) && ['human', 'stalled'].includes((p.hp_audience || '').trim());
}

// The Projects filter. Primary sessions and legacy fail-open panes only; no thread ever qualifies.
function paneHuman(p) { return primarySession(p); }

function effectiveProject(p) {
  return (p.hp_project || '').trim() || (p.project || '').trim()
    || (p.workspace_label || '').trim() || 'unknown';
}

function projectKey(p) { return `${p.host || 'local'}|${effectiveProject(p)}`; }
function paneIdentity(p) { return `${p.host || 'local'}|${p.pane_id || ''}`; }

function threadName(p) {
  const id = (p.hp_group || '').split('!').pop() || '';
  return /^t-\d+$/.test(id) ? id : p.label || p.pane_id;
}

// Escalated workers grouped by the project+host they belong to. A project with no live
// coordinator gets no badge and no standalone worker card -- its threads remain reachable only in
// All panes, which is the deliberate scope.
function followupsByProject(list) {
  const byProject = new Map();
  for (const p of list) {
    if (!escalatedThread(p)) continue;
    const key = projectKey(p);
    if (!byProject.has(key)) byProject.set(key, []);
    byProject.get(key).push(p);
  }
  return byProject;
}

// The single coordinator that owns a project's follow-ups: the earliest live coordinator in relay
// order, so several coordinators stay separate cards but a count is never shown twice.
function owningCoordinator(list, key) {
  let owner = null;
  for (const p of list) {
    if ((p.hp_role || '').trim() !== 'coordinator' || projectKey(p) !== key) continue;
    if (owner === null || (p.order ?? Infinity) < (owner.order ?? Infinity)) owner = p;
  }
  return owner;
}

// Attach, to every primary pane, the number of escalated worker threads its project owns and
// whether this pane is the coordinator that surfaces them. Pure data; rendering stays in cards.js.
function withFollowups(list) {
  const byProject = followupsByProject(list);
  const owners = new Set();
  const counts = new Map();
  for (const [key, threads] of byProject) {
    const coordinator = owningCoordinator(list, key);
    if (!coordinator) continue;
    owners.add(paneIdentity(coordinator));
    counts.set(paneIdentity(coordinator), threads.length);
  }
  return list.map(p => ({
    ...p,
    followupCount: counts.get(paneIdentity(p)) || 0,
    ownsFollowups: owners.has(paneIdentity(p)),
  }));
}

// A primary pane lands in Needs you when it is itself blocked, or when it owns one or more
// escalated worker threads. The aggregate -- not a faked process status -- drives the bucket.
function projectBucket(p) {
  if (p.ownsFollowups) return 'needs';
  return bucketOf(p);
}

// ---- Attention: chime + human timeline are primary-session only -----------------------------
//
// A worker thread never chimes or enters the default timeline on its own. A batch of newly
// escalated threads in one project produces exactly one cue and one coordinator timeline entry,
// keyed by the project so a repaint/snapshot upgrade cannot re-fire it.
let previousAttention = {};
let previousFollowups = new Map(); // projectKey -> Set(thread pane identities)

function attentionKind(p) {
  if (primarySession(p) && p.status === 'blocked') return 'blocked';
  return '';
}

function trackAttention(p) {
  const key = paneIdentity(p);
  const previous = previousAttention[key] || '';
  const current = attentionKind(p);
  if (current === previous) return false;
  previousAttention[key] = current;
  if (!current) return false;
  if (window.cue) cue('chime');
  recordTimeline(p, p.status || 'blocked');
  return true;
}

// Snapshot-only reconciliation of worker escalations onto their coordinator. Returns the number
// of newly escalated projects, so the caller can chime once per project rather than per thread.
function trackThreadAggregates(list) {
  const byProject = followupsByProject(list);
  let newProjects = 0;
  const next = new Map();
  for (const [key, threads] of byProject) {
    const ids = new Set(threads.map(paneIdentity));
    const coordinator = owningCoordinator(list, key);
    // Mirror the relay: a project with no live coordinator tracks nothing, so a coordinator
    // appearing later still fires the one aggregate cue.
    if (!coordinator) continue;
    next.set(key, ids);
    const before = previousFollowups.get(key);
    const fresh = before ? [...ids].filter(id => !before.has(id)) : [...ids];
    if (fresh.length) {
      newProjects += 1;
      if (window.cue) cue('chime');
      recordTimeline({...coordinator, status: 'blocked'}, 'blocked', threads.length);
    }
  }
  previousFollowups = next;
  return newProjects;
}

// The default timeline is a human activity feed of primary sessions. Every event is also kept in
// the raw feed for All panes; a thread event lands only there. `followups` annotates a coordinator
// aggregate entry.
function recordTimeline(p, status, followups = 0) {
  const event = {project: effectiveProject(p), agent: p.agent || '',
                 role: p.hp_role || '', label: p.label || p.pane_id,
                 status, followups, time: new Date()};
  rawTimeline.unshift(event);
  if (rawTimeline.length > 100) rawTimeline.pop();
  if (!paneHuman(p)) return;
  timeline.unshift(event);
  if (timeline.length > 100) timeline.pop();
}
