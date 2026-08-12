/**
 * Scroll only the pane that owns the conversation history. `scrollIntoView`
 * walks every scrollable ancestor and can move the iframe document itself,
 * exposing empty page space below a viewport-sized application.
 *
 * @param {{scrollHeight: number, scrollTo: Function} | null | undefined} container
 * @param {'auto' | 'smooth'} [behavior]
 * @returns {boolean}
 */
export const scrollContainerToBottom = (container, behavior = 'smooth') => {
  if (!container || typeof container.scrollTo !== 'function') return false;
  container.scrollTo({ top: container.scrollHeight, behavior });
  return true;
};

/**
 * Focus a control without asking the browser to scroll the iframe document.
 *
 * @param {{focus: Function} | null | undefined} control
 * @returns {boolean}
 */
export const focusWithoutDocumentScroll = (control) => {
  if (!control || typeof control.focus !== 'function') return false;
  control.focus({ preventScroll: true });
  return true;
};
