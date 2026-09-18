import { t, formatNumber } from '../i18n.js';
import { element } from '../render.js';

// Zone de transparence : en direct pendant le tour, repliée ensuite (CDC §11).
export function renderTransparency(live, { collapsed = false } = {}) {
  const box = element('div', 'transparency');
  if (collapsed) {
    const details = element('details');
    details.append(element('summary', null, t('ui.transparency.see_detail')));
    details.append(body(live));
    box.append(details);
    return box;
  }
  box.append(head(live));
  box.append(body(live));
  return box;
}

function head(live) {
  const row = element('div', 'transparency__head');
  const counter = live.maxIterations
    ? t('ui.transparency.iteration', { index: live.iteration, max: live.maxIterations })
    : t('ui.transparency.starting');
  row.append(element('strong', null, counter));
  const elapsed = Math.round((Date.now() - live.startedAt) / 1000);
  row.append(
    element('span', 'muted', t('ui.transparency.elapsed', {
      elapsed,
      max: Math.round(live.maxSeconds || 0),
    })),
  );
  return row;
}

function body(live) {
  const wrapper = element('div', 'transparency__body');
  if (live.maxSeconds) {
    const bar = element('div', 'transparency__progress');
    const fill = element('div', 'transparency__bar');
    const ratio = Math.min(1, (Date.now() - live.startedAt) / 1000 / live.maxSeconds);
    fill.style.width = `${Math.round(ratio * 100)}%`;
    bar.append(fill);
    wrapper.append(bar);
  }
  if (live.step) {
    const step = element(
      'div',
      `transparency__step ${live.slow ? 'transparency__step--slow' : ''}`.trim(),
    );
    step.append(element('span', null, live.step));
    wrapper.append(step);
  }
  if (live.files.length) {
    const files = element('div', 'transparency__files');
    files.append(element('span', 'muted', t('ui.transparency.files', {
      count: formatNumber(live.files.length),
    })));
    for (const file of live.files.slice(-40)) {
      files.append(element(
        'span',
        `transparency__file ${file.fromCache ? 'transparency__file--cached' : ''}`.trim(),
        file.path,
      ));
    }
    wrapper.append(files);
  }
  for (const error of live.errors) {
    wrapper.append(element('div', 'warning', error));
  }
  if (live.ignored.length) {
    const details = element('details');
    details.append(element('summary', null, t('ui.transparency.ignored', {
      count: formatNumber(live.ignored.length),
    })));
    for (const entry of live.ignored.slice(0, 200)) {
      details.append(element('div', 'muted', `${entry.path} — ${entry.label || entry.reason}`));
    }
    wrapper.append(details);
  }
  return wrapper;
}
