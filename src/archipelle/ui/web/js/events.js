// Réception des événements du worker, filtrés par run_id (CDC §11).
import { state, update, emptyLive } from './store.js';

const handlers = new Map();

export function on(type, handler) {
  handlers.set(type, handler);
}

export function dispatch(event) {
  if (state.run && event.run_id && event.run_id !== state.run.run_id && event.type !== 'app_notice') {
    return; // événement d'un tour abandonné
  }
  const handler = handlers.get(event.type);
  if (handler) handler(event.payload || {}, event);
}

export function install() {
  window.archipelleEvent = (event) => {
    try {
      dispatch(event);
    } catch (error) {
      console.error('Événement non traité', error);
    }
  };
}

export function startLive(run) {
  update({ run, live: emptyLive(run) });
}

export function patchLive(patch) {
  if (!state.live) return;
  update({ live: { ...state.live, ...patch } });
}
