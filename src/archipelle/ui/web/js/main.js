// Démarrage de l'interface : charge l'état, câble les actions et les événements.
import { call, ready } from './api.js';
import * as events from './events.js';
import { formatNumber, install as installTexts, t } from './i18n.js';
import { installLinkGuard } from './render.js';
import { isBusy, state, update } from './store.js';
import { renderComposer } from './views/composer.js';
import { confirm, linkDialog, show, toast } from './views/dialogs.js';
import { renderHeader } from './views/header.js';
import { renderOnboarding } from './views/onboarding.js';
import { renderSettings, renderAdvanced } from './views/settings.js';
import { renderSidebar } from './views/sidebar.js';
import { renderThread } from './views/thread.js';

let timer = null;

function report(result) {
  if (result && result.ok === false) toast(result.message || t('ui.errors.unknown'), 'error');
  return result;
}

const actions = {
  async newConversation() {
    const result = report(await call('new_conversation'));
    if (!result.ok) return;
    update({ conversation: result.conversation, items: [], live: null, draft: '' });
    await actions.refreshList();
  },

  async openConversation(id) {
    const result = report(await call('open_conversation', id));
    if (!result.ok) return;
    update({ conversation: result.conversation, items: result.items, live: null, draft: '' });
  },

  startRename(id) {
    update({ renamingId: id });
  },

  finishRename(title) {
    const id = state.renamingId;
    update({ renamingId: null });
    if (id && title !== null) actions.renameConversation(id, title);
  },

  async renameConversation(id, title) {
    const result = report(await call('rename_conversation', id, title));
    if (!result.ok) return;
    if (state.conversation && state.conversation.id === id) {
      update({ conversation: result.conversation, conversations: result.conversations });
    } else {
      update({ conversations: result.conversations });
    }
  },

  async deleteConversation(id) {
    const result = report(await call('delete_conversation', id));
    if (!result.ok) return;
    update({ conversations: result.conversations });
    if (state.conversation && state.conversation.id === id) {
      if (result.conversations.length) await actions.openConversation(result.conversations[0].id);
      else await actions.newConversation();
    }
  },

  async refreshList() {
    const result = await call('list_conversations', state.search);
    if (result.ok) update({ conversations: result.conversations });
  },

  search(query) {
    update({ search: query });
    actions.refreshList();
  },

  draft(text) {
    state.draft = text; // pas de rendu : le champ est déjà à jour
  },

  toggleSidebar() {
    const collapsed = !state.settings.sidebar_collapsed;
    update({ settings: { ...state.settings, sidebar_collapsed: collapsed } });
    call('update_settings', { sidebar_collapsed: collapsed });
  },

  openScreen(screen) {
    update({ screen });
  },

  async setProvider(providerId) {
    applyConversation(await call('set_provider', state.conversation.id, providerId));
  },

  async setModel(modelId) {
    applyConversation(await call('set_model', state.conversation.id, modelId));
  },

  async setMode(mode) {
    applyConversation(await call('set_mode', state.conversation.id, mode));
  },

  async setScope(scope) {
    applyConversation(await call('set_scope', state.conversation.id, scope));
  },

  async chooseScope() {
    applyConversation(await call('choose_scope', state.conversation.id));
  },

  async chooseWorkdir() {
    applyConversation(await call('choose_workdir', state.conversation.id));
  },

  async send() {
    const question = state.draft.trim();
    if (!question || isBusy()) return;
    const conversation = state.conversation;
    update({
      items: [...state.items, { id: `local-${Date.now()}`, role: 'user', text: question }],
      draft: '',
    });
    const result = report(await call('send', conversation.id, question));
    if (!result.ok) {
      if (result.key === 'agent.errors.missing_key') actions.askKey(conversation.provider_id);
      return;
    }
    startRun(result);
  },

  async continueDeeper() {
    const result = report(await call('continue_deeper', state.conversation.id));
    if (result.ok) startRun(result);
  },

  async stop() {
    report(await call('stop'));
  },

  async openSource(path) {
    report(await call('open_source', state.conversation.id, path));
  },

  async turnDetails(turnId) {
    return call('turn_details', turnId);
  },

  askKey(providerId) {
    const provider = state.providers.find((item) => item.id === providerId);
    const input = document.createElement('input');
    input.className = 'input';
    input.type = 'password';
    input.placeholder = t('ui.settings.key_placeholder');
    show({
      title: t('ui.dialog.key_title', { provider: provider ? provider.label : providerId }),
      body: input,
      actions: [
        { label: t('ui.common.cancel') },
        {
          label: t('ui.common.save'),
          variant: 'button--primary',
          onClick: () => actions.saveKey(providerId, input.value),
        },
      ],
    });
  },

  async saveKey(providerId, key) {
    const result = report(await call('save_key', providerId, key));
    if (result.ok) await refreshSettings();
  },

  async deleteKey(providerId) {
    await call('delete_key', providerId);
    await refreshSettings();
  },

  async updateSettings(patch) {
    const result = report(await call('update_settings', patch));
    if (result.ok) {
      update({ settings: result.settings, providers: result.providers });
      applyTheme();
    }
  },

  async chooseDefaultWorkdir() {
    const result = report(await call('choose_default_workdir'));
    if (result.ok && result.settings) update({ settings: result.settings });
  },

  async setCustomUrl(url) {
    const check = await call('check_custom_url', url);
    if (!check.ok) return report(check);
    if (check.cleartext_remote) toast(t('ui.settings.cleartext_warning'), 'error');
    return actions.updateSettings({
      custom_provider: { base_url: url, models: state.settings.custom_provider.models },
    });
  },

  setCustomModels(raw) {
    const models = raw.split(',').map((id) => id.trim()).filter(Boolean).map((id) => ({ id }));
    return actions.updateSettings({
      custom_provider: { base_url: state.settings.custom_provider.base_url, models },
    });
  },

  setExtraModels(providerId, raw) {
    const models = raw
      .split(',')
      .map((id) => id.trim())
      .filter(Boolean)
      .map((id) => ({ id }));
    const all = { ...(state.settings.extra_models || {}) };
    if (models.length) all[providerId] = models;
    else delete all[providerId];
    return actions.updateSettings({ extra_models: all });
  },

  setModeModel(providerId, mode, modelId) {
    const modes = { ...(state.settings.mode_models || {}) };
    modes[providerId] = { ...(modes[providerId] || {}), [mode]: modelId || null };
    return actions.updateSettings({ mode_models: modes });
  },

  setExclusions(raw) {
    const lines = raw.split('\n').map((line) => line.trim()).filter(Boolean);
    const patterns = lines.filter((line) => line.includes('*') || line.includes('.'));
    const dirs = lines.filter((line) => !patterns.includes(line));
    return actions.updateSettings({
      scan_exclusions: {
        dir_names: dirs,
        file_patterns: patterns,
        hide_dotfiles: state.settings.scan_exclusions.hide_dotfiles,
      },
    });
  },

  async purge(kind) {
    const method = { cache: 'purge_cache', history: 'purge_history', logs: 'purge_logs' }[kind];
    const result = report(await call(method));
    if (!result.ok) return;
    if (kind === 'history') {
      update({ conversations: [], items: [], conversation: null });
      await actions.newConversation();
    }
    await refreshStorage();
  },

  async finishOnboarding(key) {
    if (key && key.trim()) {
      await call('save_key', state.settings.default_provider, key.trim());
    }
    await actions.updateSettings({ onboarding_done: true });
    update({ screen: null });
    await actions.newConversation();
  },
};

function applyConversation(result) {
  report(result);
  if (result.ok && result.conversation) update({ conversation: result.conversation });
}

function startRun(result) {
  if (result.conversation) update({ conversation: result.conversation });
  events.startLive(result.run);
  refreshItems();
}

async function refreshItems() {
  if (!state.conversation) return;
  const result = await call('open_conversation', state.conversation.id);
  if (result.ok) update({ conversation: result.conversation, items: result.items });
}

async function refreshSettings() {
  const result = await call('bootstrap');
  if (result.ok) update({ settings: result.settings, providers: result.providers });
}

async function refreshStorage() {
  const result = await call('storage_info');
  if (result.ok) update({ storage: result });
}

function applyTheme() {
  document.documentElement.dataset.theme = state.settings.theme || 'light';
}

// --- événements du worker (CDC §11) ---------------------------------------------

function wireEvents() {
  events.on('turn_started', (payload) => {
    events.patchLive({
      maxIterations: payload.max_iterations,
      maxSeconds: payload.max_seconds,
      turnId: payload.turn_id,
    });
  });
  events.on('iteration', (payload) => {
    events.patchLive({ iteration: payload.index, maxIterations: payload.max });
  });
  events.on('step_started', (payload) => {
    events.patchLive({
      step: t('ui.transparency.step_tool', { tool: payload.tool, target: payload.target || '' }),
      slow: payload.tool === 'read_file' || payload.tool === 'search_fulltext',
    });
  });
  events.on('step_progress', (payload) => {
    events.patchLive({ step: t(payload.key, payload), slow: true });
  });
  events.on('file_consulted', (payload) => {
    if (!state.live) return;
    const files = state.live.files.filter((file) => file.path !== payload.path);
    events.patchLive({ files: [...files, { path: payload.path, fromCache: payload.from_cache }] });
  });
  events.on('file_ignored', (payload) => {
    if (!state.live) return;
    events.patchLive({ ignored: [...state.live.ignored, payload] });
  });
  events.on('error', (payload) => {
    if (payload.recoverable) {
      events.patchLive({
        errors: [...(state.live ? state.live.errors : []), t('ui.transparency.retry', {
          message: payload.message,
          wait: formatNumber(Math.round(payload.wait || 0)),
        })],
      });
    } else {
      toast(payload.message, 'error');
    }
  });
  events.on('final_answer', () => {
    refreshItems();
  });
  events.on('turn_interrupted', () => {
    toast(t('ui.chat.interrupted'));
  });
  events.on('turn_ended', () => {
    update({ run: null, live: null });
    refreshItems();
    actions.refreshList();
  });
  events.on('app_notice', (payload) => {
    toast(t(payload.key));
  });
  events.install();
}

// --- rendu ------------------------------------------------------------------------

function render() {
  const app = document.getElementById('app');
  const screen = document.getElementById('screen');
  const onboarding = !state.settings.onboarding_done;
  if (onboarding || state.screen) {
    app.hidden = onboarding;
    if (onboarding) renderOnboarding(actions);
    else if (state.screen === 'settings') renderSettings(actions);
    else if (state.screen === 'advanced') renderAdvanced(actions);
  } else {
    screen.hidden = true;
    screen.replaceChildren();
    app.hidden = false;
  }
  if (app.hidden) return;
  app.dataset.sidebar = state.settings.sidebar_collapsed ? 'collapsed' : 'open';
  renderSidebar(actions);
  renderHeader(actions);
  renderThread(actions);
  renderComposer(actions);
}

function scheduleRender() {
  if (timer) return;
  timer = setTimeout(() => {
    timer = null;
    render();
  }, 16);
}

export async function start() {
  installLinkGuard(linkDialog);
  wireEvents();
  await ready();
  const result = await call('bootstrap');
  if (!result.ok) {
    document.body.textContent = result.message || 'Erreur de démarrage';
    return;
  }
  installTexts(result.texts);
  update({
    settings: result.settings,
    providers: result.providers,
    conversations: result.conversations,
    ocrAvailable: result.ocr_available,
    platform: result.platform,
  });
  applyTheme();
  if (!result.ocr_available) toast(t('ui.notice.no_ocr'));
  if (result.settings.onboarding_done) {
    if (result.conversations.length) await actions.openConversation(result.conversations[0].id);
    else await actions.newConversation();
  }
  await refreshStorage();
  subscribeRender();
  render();
  // Rafraîchit le compteur de temps de la zone de transparence.
  setInterval(() => {
    if (state.live) scheduleRender();
  }, 1000);
}

function subscribeRender() {
  import('./store.js').then(({ subscribe }) => subscribe(scheduleRender));
}

export { actions, render, confirm };

start();
