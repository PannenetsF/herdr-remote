// ---- Human audience: projects own their workers -----------------------------
//
// Keep the raw `agents` array intact: the terminal, sibling navigation and All panes still address
// every live pane. Only human-facing surfaces go through this projection. Authoritative hp_* tokens
// win over names and directories; older hosts fail open so a plain session never disappears.
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

function humanThread(p) {
  return (p.hp_role || '').trim() === 'thread'
    && ['human', 'stalled'].includes((p.hp_audience || '').trim());
}

function paneHuman(p) { return primarySession(p) || humanThread(p); }

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

// Audience transitions can arrive in a full snapshot without another `blocked` frame. Track them
// independently of prompt repaints so internal -> stalled still produces exactly one local cue and
// one human timeline event. Primary sessions enter the same ledger only while actually blocked.
let previousAttention = {};
function attentionKind(p) {
  if (humanThread(p)) return p.hp_audience;
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

// The default timeline is a human activity feed, not a trace of every internal worker. All panes
// opts back into the raw feed without changing how the underlying records are merged.
function recordTimeline(p, status) {
  const event = {project: effectiveProject(p), agent: p.agent || '',
                 role: p.hp_role || '', label: p.label || p.pane_id,
                 status, time: new Date()};
  rawTimeline.unshift(event);
  if (rawTimeline.length > 100) rawTimeline.pop();
  if (!paneHuman(p)) return;
  timeline.unshift(event);
  if (timeline.length > 100) timeline.pop();
}
