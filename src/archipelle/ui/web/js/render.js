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

// Repères provisoires, dans une zone Unicode privée : le rendu Markdown les laisse intacts,
// contrairement à une balise HTML qu'il neutraliserait (protection contre le HTML des
// documents). La balise réelle n'est insérée qu'après ce rendu.
const OPEN = '\uE000';
const CLOSE = '\uE001';
const PLACEHOLDER = /\uE000(\d+)\uE001/g;

// Remplace chaque [[chemin]] par un repère provisoire, et renvoie aussi la liste des
// chemins dans l'ordre de leur numéro.
export function replaceCitations(text, sources = []) {
  const extra = [];
  const paths = [];
  const numberOf = (path) => {
    const known = sources.findIndex((source) => normalizePath(source.path) === path);
    if (known >= 0) return known + 1;
    if (!extra.includes(path)) extra.push(path);
    return sources.length + extra.indexOf(path) + 1;
  };
  const withMarks = String(text)
    .replace(new RegExp(`[${OPEN}${CLOSE}]`, 'g'), '') // aucun repère venu du texte lui-même
    .replace(CITATION, (whole, raw) => {
      const path = normalizePath(raw);
      if (!path) return '';
      const number = numberOf(path);
      paths[number] = path;
      return `${OPEN}${number}${CLOSE}`;
    });
  return { text: withMarks, paths };
}

function escapeAttribute(value) {
  return value
    .replace(/&/g, '&amp;')
    .replace(/"/g, '&quot;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

export function renderAnswer(text, sources = []) {
  const { text: marked, paths } = replaceCitations(text, sources);
  const html = markdown ? markdown.render(marked) : escapeAttribute(marked);
  const withCitations = html.replace(PLACEHOLDER, (whole, number) => {
    const path = paths[Number(number)] || '';
    return `<span class="citation" data-path="${escapeAttribute(path)}">[${number}]</span>`;
  });
  if (!window.DOMPurify) return '';       // sans désinfectant, on n'affiche pas de HTML
  return window.DOMPurify.sanitize(withCitations, {
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
