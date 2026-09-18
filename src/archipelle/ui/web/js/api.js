// Enveloppe des appels vers Python. Chaque réponse a la forme {ok: bool, ...}.
const pending = [];

function bridge() {
  return window.pywebview && window.pywebview.api ? window.pywebview.api : null;
}

export function ready() {
  const existing = bridge();
  if (existing) return Promise.resolve(existing);
  return new Promise((resolve) => {
    pending.push(resolve);
    window.addEventListener('pywebviewready', () => {
      while (pending.length) pending.shift()(bridge());
    }, { once: true });
  });
}

export async function call(method, ...args) {
  const api = await ready();
  if (!api || typeof api[method] !== 'function') {
    return { ok: false, key: 'ui.errors.bridge', message: `Méthode indisponible : ${method}` };
  }
  try {
    const result = await api[method](...args);
    return result === undefined || result === null ? { ok: true } : result;
  } catch (error) {
    return { ok: false, key: 'ui.errors.bridge', message: String(error) };
  }
}
