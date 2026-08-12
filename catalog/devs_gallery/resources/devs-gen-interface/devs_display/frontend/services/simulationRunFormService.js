/**
 * Run-form state is derived only from values declared by the generated
 * runner. The UI must not guess a plausible simulation scenario.
 */

const hasOwn = (object, key) => Object.prototype.hasOwnProperty.call(object, key);

/** @param {{default?: string | number | boolean}} parameter */
export const hasSuggestedValue = parameter => parameter.default !== undefined;

/**
 * Preserve values the student has already edited while adopting defaults for
 * newly discovered parameters. Missing defaults remain visibly empty.
 *
 * @param {Array<{name: string, type?: string, default?: string | number | boolean}>} parameters
 * @param {Record<string, string | number | boolean>} previous
 */
export const initializeScenarioValues = (parameters, previous = {}) => Object.fromEntries(
  parameters.map(parameter => [
    parameter.name,
    hasOwn(previous, parameter.name)
      ? previous[parameter.name]
      : hasSuggestedValue(parameter) ? parameter.default : ''
  ])
);

/**
 * Reset only values that actually have generated suggestions. Student-owned
 * required inputs are preserved because there is no safe value to restore.
 *
 * @param {Array<{name: string, default?: string | number | boolean}>} parameters
 * @param {Record<string, string | number | boolean>} current
 */
export const resetSuggestedValues = (parameters, current) => {
  const next = { ...current };
  parameters.forEach(parameter => {
    if (hasSuggestedValue(parameter)) next[parameter.name] = parameter.default;
  });
  return next;
};

/**
 * @param {Array<{name: string, type?: string, default?: string | number | boolean}>} parameters
 * @param {Record<string, string | number | boolean>} current
 */
export const suggestedValuesChanged = (parameters, current) => parameters.some(
  parameter => {
    if (!hasSuggestedValue(parameter)) return false;
    const value = current[parameter.name];
    if (value === parameter.default) return false;
    if (
      (parameter.type === 'integer' || parameter.type === 'number')
      && value !== ''
      && Number.isFinite(Number(value))
    ) {
      return Number(value) !== Number(parameter.default);
    }
    return true;
  }
);

/** @param {unknown} value */
export const isMissingScenarioValue = value => value === '' || value === undefined || value === null;

/**
 * @param {Array<{name: string, required?: boolean}>} parameters
 * @param {Record<string, string | number | boolean>} current
 */
export const missingRequiredParameters = (parameters, current) => parameters.filter(
  parameter => parameter.required && isMissingScenarioValue(current[parameter.name])
);
