const SUFFIX_PATTERN = /^[1-9]\d*-[1-9]\d*$/;

function normalizeSuffix(value) {
  const text = String(value || '').trim();
  return SUFFIX_PATTERN.test(text) ? text : '';
}

function chooseNextSuffix(expected, completed) {
  const done = new Set(completed || []);
  return (expected || []).find((suffix) => !done.has(suffix)) || '';
}

module.exports = {
  chooseNextSuffix,
  normalizeSuffix,
};
