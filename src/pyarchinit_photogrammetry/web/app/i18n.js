// The words of the page in its two languages. The titles of the layers and of the attributes come from site.json.

export const WORDS = {
  it: {
    layers: 'Livelli', measure: 'Misura', distance: 'Distanza', area: 'Area', stop: 'Fine',
    info: 'Informazioni', hint: 'Un clic sulla pianta: coordinate, quota e attributi.',
    measureHint: 'Clic per ogni vertice, doppio clic per chiudere.',
    x: 'X (m)', y: 'Y (m)', height: 'Quota (m)', noHeight: 'nessuna',
    length: 'Lunghezza', surface: 'Superficie', perimeter: 'Perimetro',
    local: 'Coordinate locali: origine al centro del rilievo, quote relative.',
    crs: 'Sistema di riferimento', loading: 'Caricamento…',
    failed: 'La pagina non ha potuto leggere i dati del rilievo.',
    made: 'Fatto con', language: 'English', fit: 'Tutto il rilievo',
  },
  en: {
    layers: 'Layers', measure: 'Measure', distance: 'Distance', area: 'Area', stop: 'Done',
    info: 'Information', hint: 'Click the plan: coordinates, height and attributes.',
    measureHint: 'Click for each vertex, double click to end.',
    x: 'X (m)', y: 'Y (m)', height: 'Height (m)', noHeight: 'none',
    length: 'Length', surface: 'Surface', perimeter: 'Perimeter',
    local: 'Local coordinates: origin at the centre of the survey, relative heights.',
    crs: 'Reference system', loading: 'Loading…',
    failed: 'The page could not read the data of the survey.',
    made: 'Made with', language: 'Italiano', fit: 'Whole survey',
  },
};

// A title written as {it: ..., en: ...} (or as plain text) in the language asked for.
export function pick(title, language) {
  if (title === null || title === undefined) return '';
  if (typeof title === 'string') return title;
  return title[language] || title.it || title.en || Object.values(title)[0] || '';
}
