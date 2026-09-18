// État de l'interface : objet observable minimal, sans dépendance.
const listeners = new Set();

export const state = {
  settings: {},
  providers: [],
  conversations: [],
  conversation: null,
  items: [],
  ocrAvailable: true,
  platform: 'linux',
  run: null,          // tour en cours : {run_id, conversation_id, mode, model}
  live: null,         // transparence du tour en cours
  screen: null,       // 'onboarding' | 'settings' | 'advanced' | null
  search: '',
  renamingId: null,   // conversation dont le titre est en cours d'édition
  draft: '',
};

export function subscribe(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function update(patch) {
  Object.assign(state, patch);
  for (const listener of listeners) listener(state);
}

// Verrouillage pendant un tour (CDC §7bis) : seule la conversation traitée est gelée.
export function isBusy(conversationId = state.conversation && state.conversation.id) {
  return Boolean(state.run && state.run.conversation_id === conversationId);
}

export function emptyLive(run) {
  return {
    runId: run.run_id,
    conversationId: run.conversation_id,
    iteration: 0,
    maxIterations: 0,
    startedAt: Date.now(),
    maxSeconds: 0,
    step: null,
    slow: false,
    files: [],
    ignored: [],
    errors: [],
    done: false,
  };
}
