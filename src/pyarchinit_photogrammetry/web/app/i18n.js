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
    noRanges: 'Questo server non dà i file a pezzi (richieste HTTP Range): le immagini e le quote sono '
      + 'scaricate per intero, e la pagina è più lenta.',
    noRangesLeft: 'Troppo grandi per essere scaricati per intero, questi livelli non sono mostrati. '
      + 'Serve un server che risponda alle richieste Range:',
    made: 'Fatto con', language: 'English', fit: 'Tutto il rilievo',
    clear: 'Cancella', plan: 'Pianta', model3d: 'Modello 3D', home: 'Vista iniziale', model: 'Modello',
    hint3d: 'Un clic sul modello: coordinate e quota del punto. Trascinare per ruotare, tasto destro o due dita '
      + 'per spostare, rotella per avvicinarsi.',
    measureHint3d: 'Clic sul modello per ogni vertice, doppio clic o Fine per chiudere, Esc per annullare.',
    length3d: 'Lunghezza 3D', planLength: 'Lunghezza in pianta', rise: 'Dislivello (ultimo − primo)',
    area3d: 'Superficie 3D', planArea: 'Superficie in pianta',
    failed3d: 'Il modello 3D non può essere mostrato: il browser non ha WebGL o i dati non sono leggibili.',
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
    noRanges: 'This host does not serve files by parts (HTTP range requests): the pictures and the heights '
      + 'are downloaded whole, and the page is slower.',
    noRangesLeft: 'Too large to be downloaded whole, these layers are not shown. '
      + 'A host that answers range requests is needed:',
    made: 'Made with', language: 'Italiano', fit: 'Whole survey',
    clear: 'Clear', plan: 'Plan', model3d: '3D model', home: 'Home view', model: 'Model',
    hint3d: 'Click the model: coordinates and height of the point. Drag to rotate, right button or two fingers '
      + 'to pan, wheel to zoom.',
    measureHint3d: 'Click the model for each vertex, double click or Done to end, Esc to cancel.',
    length3d: '3D length', planLength: 'Plan length', rise: 'Height difference (last − first)',
    area3d: '3D area', planArea: 'Plan area',
    failed3d: 'The 3D model cannot be shown: the browser has no WebGL or the data cannot be read.',
  },
};

// A title written as {it: ..., en: ...} (or as plain text) in the language asked for.
export function pick(title, language) {
  if (title === null || title === undefined) return '';
  if (typeof title === 'string') return title;
  return title[language] || title.it || title.en || Object.values(title)[0] || '';
}
