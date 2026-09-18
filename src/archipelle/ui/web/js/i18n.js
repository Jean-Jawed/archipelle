// Libellés fournis par Python : aucune chaîne d'interface en dur ici (CDC §10).
let texts = {};

export function install(catalog) {
  texts = catalog || {};
}

export function t(key, params = {}) {
  const template = texts[key];
  if (template === undefined) return key;
  return template.replace(/\{(\w+)\}/g, (whole, name) =>
    Object.hasOwn(params, name) ? String(params[name]) : whole,
  );
}

export function formatNumber(value) {
  return new Intl.NumberFormat('fr-FR').format(value);
}

export function formatBytes(bytes) {
  if (bytes < 1024) return t('units.bytes', { value: bytes });
  const units = ['units.kb', 'units.mb', 'units.gb'];
  let size = bytes;
  for (const unit of units) {
    size /= 1024;
    if (size < 1024 || unit === 'units.gb') {
      return t(unit, { value: size.toFixed(1).replace('.', ',').replace(',0', '') });
    }
  }
  return String(bytes);
}

export function formatDay(timestamp) {
  const date = new Date(timestamp * 1000);
  const today = new Date();
  const start = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const days = Math.round((start(today) - start(date)) / 86400000);
  if (days <= 0) return 'ui.sidebar.today';
  if (days === 1) return 'ui.sidebar.yesterday';
  if (days <= 7) return 'ui.sidebar.week';
  return 'ui.sidebar.older';
}
