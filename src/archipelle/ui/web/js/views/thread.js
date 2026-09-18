import { t } from '../i18n.js';
import { answerBody, element, sourcesBlock } from '../render.js';
import { state } from '../store.js';
import { renderTransparency } from './transparency.js';

export function renderThread(actions) {
  const host = document.getElementById('thread');
  host.replaceChildren();
  if (!state.conversation) return;
  if (state.items.length === 0 && !state.live) {
    host.append(emptyState());
  }
  let lastTurn = null;
  for (const item of state.items) {
    if (item.role === 'user') {
      if (lastTurn && lastTurn !== item.turn_id) appendPastTurn(host, lastTurn, actions);
      lastTurn = item.turn_id;
      host.append(userMessage(item));
    } else if (item.role === 'assistant' && item.text) {
      host.append(assistantMessage(item, actions));
    } else if (item.role === 'notice') {
      host.append(noticeMessage(item));
    }
    if (item.end) host.append(turnEnd(item));
  }
  if (state.live) {
    host.append(renderTransparency(state.live));
  } else if (lastTurn) {
    appendPastTurn(host, lastTurn, actions);
  }
  host.scrollTop = host.scrollHeight;
}

function appendPastTurn(host, turnId, actions) {
  const box = element('div', 'transparency');
  const details = element('details');
  const summary = element('summary', null, t('ui.transparency.see_detail'));
  details.append(summary);
  const content = element('div', 'muted', t('ui.common.loading'));
  details.append(content);
  details.addEventListener('toggle', async () => {
    if (!details.open || details.dataset.loaded) return;
    details.dataset.loaded = '1';
    const detail = await actions.turnDetails(turnId);
    content.replaceChildren(renderPastDetail(detail));
  }, { once: false });
  box.append(details);
  host.append(box);
}

function renderPastDetail(detail) {
  const wrapper = element('div');
  if (!detail || !detail.ok) {
    wrapper.append(element('div', 'muted', t('ui.transparency.no_detail')));
    return wrapper;
  }
  for (const step of detail.steps || []) {
    if (step.kind === 'tool_call') {
      wrapper.append(element('div', 'transparency__step',
        t('ui.transparency.step_tool', { tool: step.tool, target: step.target || '' })));
    } else if (step.kind === 'iteration') {
      wrapper.append(element('div', 'muted',
        t('ui.transparency.step_iteration', { index: step.index })));
    }
  }
  if ((detail.consulted || []).length) {
    const files = element('div', 'transparency__files');
    for (const path of detail.consulted) files.append(element('span', 'transparency__file', path));
    wrapper.append(files);
  }
  for (const entry of detail.ignored || []) {
    wrapper.append(element('div', 'muted', `${entry.path} — ${entry.label}`));
  }
  return wrapper;
}

// Repère de fin : sans lui, une réponse qui s'arrête net se confond avec une recherche
// encore en cours.
function turnEnd(item) {
  const key = {
    complete: 'ui.chat.turn_done',
    partial: 'ui.chat.turn_partial',
    interrupted: 'ui.chat.turn_interrupted_meta',
  }[item.end.status] || 'ui.chat.turn_done';
  return element('div', 'turn-end muted', t(key, item.end));
}

function emptyState() {
  const box = element('div', 'card');
  box.append(element('h2', 'card__title', t('ui.chat.empty_title')));
  box.append(element('p', 'muted', t('ui.chat.empty_body')));
  return box;
}

function userMessage(item) {
  const message = element('div', 'message message--user');
  const bubble = element('div', 'message__bubble');
  bubble.append(element('div', 'message__body', item.text));
  message.append(bubble);
  return message;
}

function noticeMessage(item) {
  const message = element('div', 'message message--notice');
  const bubble = element('div', 'message__bubble');
  bubble.append(element('div', 'message__body', item.text));
  message.append(bubble);
  return message;
}

function assistantMessage(item, actions) {
  const message = element('div', 'message message--assistant');
  const bubble = element('div', 'message__bubble');
  bubble.append(answerBody(item.text, item.sources || []));
  if ((item.sources || []).length) {
    bubble.append(sourcesBlock(item.sources, actions.openSource));
  }
  message.append(bubble);
  const meta = element('div', 'message__meta');
  meta.append(element('span', 'message__dot'));
  meta.append(element('span', null, item.model || ''));
  message.append(meta);
  return message;
}
