/**
 * Choose the in-progress simulation that the UI should follow automatically.
 *
 * The backend calls generated simulations "projects".  This helper deliberately
 * works with only their public list fields so its selection policy stays small,
 * deterministic, and independently testable.
 *
 * A manual dropdown choice is authoritative.  Otherwise the newest simulation
 * whose files are still being built is the best representation of "the
 * simulation currently being generated".
 *
 * @param {Array<{
 *   project_id: string,
 *   status: string,
 *   updated_at?: string,
 *   created_at?: string,
 *   version?: number
 * }>} projects
 * @param {string | null} currentProjectId
 * @param {boolean} hasManualSelection
 * @returns {typeof projects[number] | null}
 */
export const projectToFollowDuringGeneration = (
  projects,
  currentProjectId,
  hasManualSelection
) => {
  if (hasManualSelection) {
    // A manual choice prevents auto-switching, but it must not freeze a
    // simulation that is itself still being generated.
    return projects.find(project => (
      project.project_id === currentProjectId
      && project.status === 'updating'
    )) || null;
  }

  const building = projects
    .filter(project => project.status === 'updating')
    .sort((left, right) => {
      const rightTime = Date.parse(right.updated_at || right.created_at || '') || 0;
      const leftTime = Date.parse(left.updated_at || left.created_at || '') || 0;
      if (rightTime !== leftTime) return rightTime - leftTime;

      const versionDifference = Number(right.version || 0) - Number(left.version || 0);
      if (versionDifference !== 0) return versionDifference;
      return right.project_id.localeCompare(left.project_id);
    });

  // Return the selected in-progress simulation as well. Its file tree keeps
  // changing while the agent writes later components, so treating selection
  // as "already loaded" leaves the Files tab stale until generation ends.
  return building[0] || null;
};

/**
 * Resolve the simulation reported by a completed generator request.
 * Prefer the currently selected simulation when that exact simulation changed;
 * otherwise choose the newest reported result unless the user deliberately
 * selected something else.
 *
 * @param {Array<{
 *   project_id: string,
 *   display_name: string,
 *   updated_at?: string,
 *   created_at?: string,
 *   version?: number
 * }>} projects
 * @param {string[]} reportedIdsOrNames
 * @param {string | null} currentProjectId
 * @param {boolean} hasManualSelection
 * @returns {typeof projects[number] | null}
 */
export const projectToOpenAfterGeneration = (
  projects,
  reportedIdsOrNames,
  currentProjectId,
  hasManualSelection
) => {
  const reported = projects
    .filter(project => (
      reportedIdsOrNames.includes(project.project_id)
      || reportedIdsOrNames.includes(project.display_name)
    ))
    .sort((left, right) => {
      const rightTime = Date.parse(right.updated_at || right.created_at || '') || 0;
      const leftTime = Date.parse(left.updated_at || left.created_at || '') || 0;
      if (rightTime !== leftTime) return rightTime - leftTime;
      return Number(right.version || 0) - Number(left.version || 0);
    });

  const selectedReported = reported.find(project => project.project_id === currentProjectId);
  if (selectedReported) return selectedReported;
  return hasManualSelection ? null : (reported[0] || null);
};

/**
 * Reconcile an explicit file selection with a live refresh of one simulation.
 *
 * Generation polling is allowed to replace the file contents, but it must not
 * behave like a new simulation selection. Keep the student's chosen file only
 * while the same simulation remains selected and that file still exists in
 * the refreshed snapshot. This also makes a real deletion visible instead of
 * leaving a stale source preview behind.
 *
 * @param {string | null} selectedFilePath
 * @param {string | null} currentProjectId
 * @param {string | null} refreshedProjectId
 * @param {string[]} refreshedFilePaths
 * @returns {string | null}
 */
export const selectedFileAfterProjectRefresh = (
  selectedFilePath,
  currentProjectId,
  refreshedProjectId,
  refreshedFilePaths
) => {
  if (!selectedFilePath || !currentProjectId || currentProjectId !== refreshedProjectId) {
    return null;
  }

  const normalize = value => String(value || '')
    .replace(/\\/g, '/')
    .replace(/^\.?\//, '')
    .replace(/^\/+/, '');
  const normalizedSelection = normalize(selectedFilePath);
  return refreshedFilePaths.find(path => normalize(path) === normalizedSelection) || null;
};

/**
 * A newly discovered simulation should open in Files so its build is visible.
 * Routine refreshes of the already selected simulation must respect whichever
 * Files, Structure, or Run tab the student chose afterward. This distinction
 * also matters when a student runs a simulation: backend validation temporarily
 * reports that same simulation as `updating`, but it is still the same selected
 * simulation and must not pull the UI away from Run.
 *
 * @param {string | null} currentProjectId
 * @param {string | null} refreshedProjectId
 * @returns {boolean}
 */
export const shouldFocusFilesForProjectRefresh = (
  currentProjectId,
  refreshedProjectId
) => Boolean(refreshedProjectId && currentProjectId !== refreshedProjectId);

/**
 * Decide whether a mounted Run panel must re-read its runner specification.
 *
 * The Run panel can inspect a simulation while the generator is still writing
 * it. During that interval the backend intentionally reports the runner as
 * unavailable. Completion updates the same simulation record in place, so a
 * refresh keyed only by simulation id would keep that temporary response
 * forever. Re-read only when that same simulation leaves `updating`; changing
 * simulations is already handled by the panel's normal scope reset.
 *
 * @param {{scopeKey: string, status?: string | null} | null} previous
 * @param {{scopeKey: string, status?: string | null}} current
 * @returns {boolean}
 */
export const shouldRefreshSimulationSpecAfterProjectUpdate = (
  previous,
  current
) => Boolean(
  previous
  && previous.scopeKey === current.scopeKey
  && previous.status === 'updating'
  && current.status !== 'updating'
);

/**
 * Map a session-relative activity path onto the matching path in an already
 * loaded simulation tree. Before the simulation is discoverable, callers can
 * safely keep using the original session-relative path as a one-file preview.
 *
 * @param {string} workspacePath
 * @param {string[]} projectFilePaths
 * @returns {string}
 */
export const resolveActivityPreviewPath = (workspacePath, projectFilePaths) => {
  const normalize = value => String(value || '')
    .replace(/\\/g, '/')
    .replace(/^\.?\//, '')
    .replace(/^\/+/, '');
  const normalizedWorkspacePath = normalize(workspacePath);
  const match = projectFilePaths
    .map(path => ({ path, normalized: normalize(path) }))
    .filter(({ normalized }) => (
      normalizedWorkspacePath === normalized
      || normalizedWorkspacePath.endsWith(`/${normalized}`)
    ))
    .sort((left, right) => right.normalized.length - left.normalized.length)[0];
  return match?.path || workspacePath;
};

/**
 * Convert an activity preview response into the file tree and selected path
 * used by the Files tab. New backends return a bounded snapshot rooted at the
 * generated simulation; the fallback preserves compatibility with a one-file
 * preview response.
 *
 * @param {{
 *   path: string,
 *   content: string,
 *   selected_path?: string,
 *   files?: Record<string, string>
 * }} preview
 * @returns {{files: Record<string, string>, selectedPath: string}}
 */
export const activityPreviewFileState = preview => {
  const snapshot = preview?.files && typeof preview.files === 'object'
    ? preview.files
    : {};
  const files = Object.keys(snapshot).length > 0
    ? snapshot
    : { [preview.path]: preview.content };
  const selectedPath = (
    typeof preview.selected_path === 'string'
    && Object.prototype.hasOwnProperty.call(files, preview.selected_path)
  )
    ? preview.selected_path
    : resolveActivityPreviewPath(preview.path, Object.keys(files));
  return { files, selectedPath };
};
