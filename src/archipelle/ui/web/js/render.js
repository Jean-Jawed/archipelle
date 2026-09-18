// Rendu sûr des réponses (CDC §9) : Markdown sans HTML brut, puis désinfection.
import { t } from './i18n.js';

const CITATION = /\[\[([^\[\]\n]{1,500})\]\]/g;

const markdown = window.markdownit
  ? window.markdownit({ html: false, linkify: false, breaks: true })
  : null;

export function normalizePath(raw) {
  return String(raw)
    .normalize('NFC')
    .trim()
    .replace(/\\/g, '/')
    .replace(/\/{2,}/g, '/')
    .replace(/^\.\//, '')
    .replace(/^\/+|\/+$/g, '');
}

// Remplace [[chemin]] par une référence numérotée, avant tout rendu Markdown.
export function replaceCitations(text, sources = []) {
  const order = [];
  const indexOf = (path) => {
    const known = sources.findIndex((source) => normalizePath(source.path) === path);
    if (known >= 0) return known + 1;
    if (!order.includes(path)) order.push(path);
    return sources.length + order.indexOf(path) + 1;
  };
  return String(text).replace(CITATION, (whole, raw) => {
    const path = normalizePath(raw);
    if (!path) return '';
    return `<span class="citation" data-path="${escapeAttribute(path)}">[${indexOf(path)}]</span>`;
  });
}

function escapeAttribute(value) {
  return value.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
}

export function renderAnswer(text, sources = []) {
  const withRefs = replaceCitations(text, sources);
  const html = markdown ? markdown.render(withRefs) : escapeAttribute(withRefs);
  if (!window.DOMPurify) return '';       // sans désinfectant, on n'affiche pas de HTML
  return window.DOMPurify.sanitize(html, {
    ALLOWED_ATTR: ['class', 'data-path', 'href', 'title', 'colspan', 'rowspan'],
    FORBID_TAGS: ['style', 'form', 'input', 'iframe', 'object', 'embed'],
    ADD_ATTR: ['data-path'],
  });
}

export function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

export function answerBody(text, sources) {
  const body = element('div', 'message__body');
  body.innerHTML = renderAnswer(text, sources);
  return body;
}

// Aucun lien ne s'ouvre : un clic affiche seulement l'adresse (CDC §9).
export function installLinkGuard(onLink) {
  document.addEventListener('click', (event) => {
    const link = event.target.closest && event.target.closest('a[href]');
    if (!link) return;
    event.preventDefault();
    event.stopPropagation();
    onLink(link.getAttribute('href') || '');
  }, true);
}

export function sourcesBlock(sources, onOpen) {
  const block = element('div', 'sources');
  if (!sources || sources.length === 0) return block;
  block.append(element('div', 'sources__title', t('ui.chat.sources')));
  sources.forEach((source, index) => {
    const row = element('div', source.verified ? 'source' : 'source source--unverified');
    row.append(element('span', 'muted', `[${index + 1}]`));
    if (source.verified) {
      const button = element('button', 'source__link', source.path);
      button.type = 'button';
      button.addEventListener('click', () => onOpen(source.path));
      row.append(button);
    } else {
      row.append(element('span', null, source.path));
      row.append(element('span', 'source__tag', t('ui.chat.unverified')));
    }
    block.append(row);
  });
  return block;
}
