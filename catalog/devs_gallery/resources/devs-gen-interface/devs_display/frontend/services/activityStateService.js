const TERMINAL_ACTIVITY_STATES = new Set(['completed', 'failed']);

// These stages publish their own terminal event and may legitimately remain
// active while nested agent/tool activity appears after them.
const EXPLICIT_LIFECYCLE_KEYS = new Set([
  'build_simulation',
  'plan_structure',
  'generate_components',
  'verify_model',
  'create_runner',
  'package_simulation',
  'agent_test_simulation'
]);

/**
 * Mark nonterminal history rows as completed once their work has clearly been
 * superseded. The newest activity remains active. Long-running lifecycle
 * stages remain active across nested events until their own key advances.
 *
 * @template {{activityKey: string, state: string}} T
 * @param {T[]} activities
 * @returns {T[]}
 */
export const deriveActivityTimelineStates = activities => activities.map((activity, index) => {
  if (TERMINAL_ACTIVITY_STATES.has(activity.state)) return activity;

  const later = activities.slice(index + 1);
  if (later.length === 0) return activity;

  const sameStageAdvanced = later.some(candidate => (
    candidate.activityKey === activity.activityKey
  ));
  if (sameStageAdvanced || !EXPLICIT_LIFECYCLE_KEYS.has(activity.activityKey)) {
    return { ...activity, state: 'completed' };
  }
  return activity;
});

