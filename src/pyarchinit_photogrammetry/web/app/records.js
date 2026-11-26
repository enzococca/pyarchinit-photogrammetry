// The record of a stratigraphic unit in the side panel: its title, its fields by group, its relations. A relation
// toward a unit of the site is a button (it selects that unit), one toward a unit the site does not have is text.
// Every value is written as text, never as markup.

function el(tag, text, cls) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (cls) node.className = cls;
  return node;
}

// ``record``: {title, groups: [{name, fields: [[column, value]]}], relations: [[kind, us, area, key|null]]};
// ``title(name)`` the title of a group, ``fieldName(column)`` the title of a column, ``word(key)`` a word of the
// page, ``onRelation(key)`` called by the button of a relation.
export function recordBlock(record, {title, fieldName, word, onRelation}) {
  const block = el('div', null, 'record');
  block.append(el('h3', record.title || ''));
  for (const group of record.groups || []) {
    const fields = (group.fields || []).filter(([, v]) => v !== null && v !== undefined && v !== '');
    if (!fields.length) continue;
    block.append(el('h4', title(group.name)));
    const table = el('table');
    for (const [column, value] of fields) {
      const tr = el('tr');
      tr.append(el('td', fieldName(column)), el('td', value));
      table.append(tr);
    }
    block.append(table);
  }
  const relations = record.relations || [];
  if (relations.length) {
    block.append(el('h4', word('relations')));
    const list = el('ul', null, 'relations');
    for (const [kind, us, , key] of relations) {
      const name = [kind, us].filter((x) => x !== null && x !== undefined && x !== '').join(' ');
      const item = el('li');
      if (key) {
        const button = el('button', name);
        button.type = 'button';
        button.dataset.record = key;
        button.addEventListener('click', () => onRelation(key));
        item.append(button);
      } else {
        item.append(el('span', name));
      }
      list.append(item);
    }
    block.append(list);
  }
  return block;
}
