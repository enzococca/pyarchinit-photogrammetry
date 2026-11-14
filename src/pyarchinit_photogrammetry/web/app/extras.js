// What plugins add to the page: a tab of cards (a table with its pictures) or a tab with a page of text. The text of
// a page was cleaned when the site was built; it is cleaned again here, element by element, before it is shown:
// only the tags and attributes below, links that leave the page only to http(s) and mail, pictures only among the
// files of the plugin. Every other text is written as text, never as markup.

const TAGS = {
  p: [], br: [], hr: [], h3: [], h4: [], h5: [], strong: [], b: [], em: [], i: [], u: [], small: [], sub: [],
  sup: [], code: [], pre: [], blockquote: [], span: [], div: [], ul: [], ol: [], li: [], dl: [], dt: [], dd: [],
  table: [], thead: [], tbody: [], tr: [], th: ['colspan', 'rowspan'], td: ['colspan', 'rowspan'], caption: [],
  figure: [], figcaption: [], a: ['href', 'title'], img: ['src', 'alt', 'title', 'width', 'height'],
};

function el(tag, text, cls) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (cls) node.className = cls;
  return node;
}

// The nodes of ``source`` (parsed, never attached to the page) copied into ``target`` keeping only what is allowed.
function copyClean(source, target, base) {
  for (const node of source.childNodes) {
    if (node.nodeType === Node.TEXT_NODE) {
      target.append(document.createTextNode(node.textContent));
      continue;
    }
    if (node.nodeType !== Node.ELEMENT_NODE) continue;
    const tag = node.tagName.toLowerCase();
    if (!(tag in TAGS)) {
      if (!['script', 'style', 'iframe', 'object', 'embed', 'template', 'svg', 'math'].includes(tag)) {
        copyClean(node, target, base); // an unknown wrapper: its text stays
      }
      continue;
    }
    const out = document.createElement(tag);
    for (const name of TAGS[tag]) {
      const value = node.getAttribute(name);
      if (value === null) continue;
      if (name === 'href') {
        if (!/^(https?:|mailto:)/i.test(value)) continue;
        out.setAttribute('href', value);
        out.setAttribute('rel', 'noopener noreferrer nofollow');
        out.setAttribute('target', '_blank');
      } else if (name === 'src') {
        if (!/^[A-Za-z0-9_-]+\.jpg$/.test(value)) continue;
        out.setAttribute('src', `${base}/${value}`);
      } else if (['width', 'height', 'colspan', 'rowspan'].includes(name)) {
        if (/^\d+$/.test(value)) out.setAttribute(name, value);
      } else {
        out.setAttribute(name, value);
      }
    }
    if (tag === 'img' && !out.hasAttribute('src')) continue;
    copyClean(node, out, base);
    target.append(out);
  }
}

// A page of text of a plugin (entry: {url, base}) into ``box``.
export async function showPage(box, entry) {
  const answer = await fetch(entry.url);
  if (!answer.ok) throw new Error(`${entry.url}: HTTP ${answer.status}`);
  const parsed = new DOMParser().parseFromString(await answer.text(), 'text/html');
  const page = el('div', null, 'plugin-page');
  copyClean(parsed.body, page, entry.base);
  box.append(page);
}

// A table of a plugin as cards (entry: {url}); ``title`` gives a title in the language of the page.
export async function showTable(box, entry, title) {
  const answer = await fetch(entry.url);
  if (!answer.ok) throw new Error(`${entry.url}: HTTP ${answer.status}`);
  const data = await answer.json();
  const cards = el('div', null, 'cards');
  for (const row of data.rows || []) {
    const card = el('article', null, 'card');
    if (typeof row._image === 'string' && /^[\w/.-]+\.jpg$/.test(row._image) && !row._image.includes('..')) {
      const picture = el('img');
      picture.src = row._image;
      picture.alt = '';
      picture.loading = 'lazy';
      card.append(picture);
    }
    const table = el('table');
    for (const column of data.columns || []) {
      const value = row[column.key];
      if (value === undefined || value === null || value === '') continue;
      const tr = el('tr');
      tr.append(el('td', title(column.title)), el('td', typeof value === 'object' ? JSON.stringify(value) : value));
      table.append(tr);
    }
    card.append(table);
    cards.append(card);
  }
  box.append(cards);
}
