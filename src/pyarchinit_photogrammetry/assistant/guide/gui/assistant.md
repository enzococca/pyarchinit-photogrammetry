# The assistant

The *Assistant* pane (at the bottom, beside the *Python console*; *View > Assistant > Open the assistant* or *Tools >
Assistant > Open the assistant*) is a conversation with a language model about your project and about the program,
a script editor to run on the project what the model proposes - or what you write yourself - and the actions the
model carries out on the open project when you ask it to do something.

![The Assistant pane](../img/gui/pane-assistant.png)

## What it answers with

The model reads the questions together with the passages of this guide that fit them, and it can look at the
project through tools that only read: the summary of the project, the alignment and its errors, the markers,
the DEM, the orthophoto, the dense cloud, the model, the thermal and multispectral products. It answers in the language of the interface. When the answer holds
a Python script, *Open in the editor* under it copies the script into the editor on the right.

### What it knows of the open project

Every question carries a short block, *The project now*, read from the project and the window just before the
question leaves: the chunks (photos, aligned photos, products with their versions and the active one, the rasters of
the raster calculator by name, how many units, spot heights, stones, lines and elevations they have), the active
chunk, what the 2D view shows (the orthophoto, the DEM, a raster of the calculator, a version, an elevation), what the
3D view shows, the selection (a polygon and its area, how many photos are selected) and the photo filter. Only names,
kinds and counts: never a path, a coordinate or the pixels of a raster. With it the model writes scripts for *this*
project - `chunk` is the active chunk it names, another chunk is `doc.chunk_by_label("...")`. When the 2D view shows a
raster of the kind you ask about (an orthophoto, a DEM, an elevation...), it works on that one without asking, even if
other chunks have one, unless you name another. It asks one question only when nothing suitable is shown and the
product exists in several chunks or versions ("which orthophoto: that of Area nord or that of Area sud?").

## Actions

When you ask the assistant to *do* something, it does it with an action instead of writing a script:

| Action | What it does |
| --- | --- |
| Show | the orthophoto, the DEM, the model, the cloud, an elevation or a raster of the calculator (also a kept version or another chunk) |
| Choose the photos | by area (the selected polygon), by markers, by name, all or none; then filters or selects them in the Photos pane, or switches them on or off |
| Stones (SAM) | the stone finder on the orthophoto, an elevation or a raster of the calculator (the one shown unless named; with a DEM shown, the chunk's orthophoto, and the result says so), stones drawn in the layer «Pietre» |
| Build again | the DEM, orthophoto, dense cloud, model, texture, relief, normal map, TIN or contour lines with the parameters you give; the product before is kept as a version |
| Units (SAM) | the units SAM finds in an area of the raster shown, as candidates in the layer «Unita' da SAM» |
| Export | a product or the drawings to a file: the window always asks where (and before overwriting a file); a cancelled dialog is a *No* |
| Script | a script the model writes, run as *Run* in the editor runs it |

The same functions are `ppg.actions` in a script (see [Scripts](../scripting.md)).

**When they run** - *View > Assistant > Actions*:

* **Always ask**: every action appears as a card in the conversation and waits;
* **Quick ones at once** (the default): showing on the active chunk, and filtering or selecting photos, run at once
  (a line in the chat says what ran); stones, builds, units, exports, photos switched on or off, scripts and showing
  another chunk wait for you - showing another chunk makes it the active one, which starts its undo history afresh;
* **Run everything (scripts and exports ask)**: every action runs at once except the model's scripts and the exports,
  which still wait for *Run*; the quick ones leave only a line in the chat, the others a card with the result and
  *Undo*. What a script does outside the project is not undone.

A card names the action and its parameters in words, with **Run**, **Edit** (the parameters in a small form; the card
waits again for *Run*) and **No**. The model gets what happened - done with its facts, *No*, stopped or the error - and
must say so: it never says it did something that did not run. **Stop** while a card waits or an action runs stops it.
A card names the chunk it acts on: when another project is opened, or another chunk made active, before *Run*, the
action is stopped ("stopped: project changed") and the model is told.

**Undo**: showing and filtering or selecting photos only change the view: they run at once and are not a step of
*Edit > Undo*. Every other action runs like a script, in the background with the window usable, and everything it changes in the
project is ONE entry of *Edit > Undo* named after it ("Assistant: Stones (SAM)"), also when it fails halfway; the
**Undo** button of its card is the same, while the action is the last step. Exports and the other calls outside the
project are said in the card and cannot be undone. Only one step runs at a time: an action asked while another step
runs is refused and the model is told.

`ppg assistant ask` on the command line has no window: there the assistant only reads, the actions are not offered.
The actions also need room in the model's context: at least 8 000 characters free beyond the instructions and the
project's block. With a smaller context (a local model with a short window) they are off, and the answer says so.

## The three kinds of model

| Kind | Where it runs | What it needs |
| --- | --- | --- |
| **Local** | on this computer: LM Studio, llama.cpp, Ollama, Unsloth (any server with the OpenAI chat-completions API) | the server started with a model loaded; the program looks for it on the usual ports, or uses the address you give |
| **Claude** | at Anthropic | a key; models `claude-opus-5-5` (default) or `claude-opus-5` |
| **OpenAI** | at OpenAI | a key; the models are those of your account |

A local server is only ever an address of this computer (`127.0.0.1`, `::1`, `localhost`): any other address
is refused. The program never downloads models: you load them in the server.
Some small local models write the calls to the project's tools as JSON in their answer instead of making them: the
program reads such a call from the text and runs it like a real one, and *What is sent* says "call read from the
text".

The combo at the top of the pane chooses the kind (its last entry opens the settings); the dot beside it is
**green "local"** when nothing leaves the computer and **orange "external"** when the questions go to Claude
or OpenAI.

## What leaves the computer

With a local model nothing leaves. With Claude or OpenAI the text goes through a privacy filter first:

* the names of the site, of the places and of the people of the project are replaced by placeholders
  ("Sito A", "Persona 1") and put back in the answer;
* only relative coordinates and heights are sent, the reference system as "projected, in metres" or
  "geographic"; file, chunk and project names become neutral;
* photos, GPS and GNSS positions are never sent;
* the block *The project now* goes through the same filter: chunk, raster and version names become neutral names,
  counts and kinds stay; the names the model uses in an action are turned back into the real ones before it runs, and
  what the action answers goes back through the filter.

The filter cannot recognise what you type yourself: before the first question of an external conversation an
orange line says so - **do not write coordinates or names in the question**. Names of versions and rasters you type
reach an external model as you typed them, as they do with the statistics tools. *What is sent* shows, question by
question, the exact text that left the computer, and the table of the names the filter replaced (real name,
sent as).

A question that cannot be answered - the local server stopped, too slow, a key refused - ends with a red line
that names the address or the reason; the pane stays usable. A call the model already made in the same answer is not run again (*What is sent* says "already called"), and a model that only repeats its calls is asked to answer. Tool results are cut at 4000 characters and, with a local model, only the last 12 are sent in full (the older ones as "(result omitted)"), within a budget of 80 000 characters a request (`assistant/context_chars` in the settings). When the server says the conversation exceeds the context of the model, the oldest results are left out once more; if that is not enough, start a new conversation. *Stop* ends a question while the model is still
writing; *New conversation* forgets the history. Choosing another model (kind, model or address) or opening
another project also starts a new conversation: the chat is cleared with a line saying why, so the chat and
*What is sent* always tell the same conversation.

## The script editor

On the right: write or paste a script (Tab = 4 spaces), then **Run**. The script sees the names of the Python
console (`doc`, `chunk`, `window`, `np`, `ppg`, `polygon`, `cameras`, `progress`) and runs in the background
like every processing step: the window stays usable, the progress window shows what it prints, and *Stop*
ends it. What it prints and its error, if any, are in the box under the editor.

* **Undo the script**: everything the script changed in the project - every chunk, the products it rebuilt
  (the previous ones come back), the files it wrote in the project's folder, the chunks it added or removed -
  goes back in one step, also when it stopped halfway with an error. It is the same entry as *Edit > Undo*.
  What it did outside the project (PyArchInit, the live GeoPackage of QGIS, exports) is said after the run and
  cannot be undone.
* **Error? Ask the assistant**: after an error, the script and its traceback go to the model, which answers
  with the corrected script.
* **Save as .py**: the script in a file, to run again with *Tools > Run Python script...*.

A script of the editor only runs when you click *Run*; what the assistant runs by itself are the actions above, as
*View > Assistant > Actions* allows. See [Scripts](../scripting.md) for what a script can do.

## Settings

*Tools > Assistant > Assistant settings...* (or *Settings...* in the pane):

* **Local**: the address of the server (*Find the servers* tries the usual ports) and the model (*Refresh the
  models* reads the list from the server);
* **Claude**: the model, and the key;
* **OpenAI**: the model (*Refresh the models* reads those of your account), and the key;
* **Test the connection** asks the server or the service for its models;
* the **last evaluation** of each kind: under 2 the kind is "limited for scripts" - its answers are fine to
  read, its scripts need a careful look.

The keys are kept only in the keychain of the system (*Save in the keychain*, *Forget*) or read from the
environment variables `ANTHROPIC_API_KEY` and `OPENAI_API_KEY`: never in the settings, the project or the
logs.

## The evaluation

How well a model works with the program is measured on a fixed set of 8 questions and 6 script tasks on the demo
project (`ppg demo FOLDER --process`): the questions about the project (DEM, orthophoto, alignment and control points,
markers) and about the program (contour lines, IFC, confidence, holes in the DEM); the scripts the region on three
markers, the normal map, a chunk with the photos of an area, the photos outside an area disabled, a DEM of the ground
only, the ground points exported. Each script runs on a copy of the project and is checked; after an error the model
gets one second try, as with *Error? Ask the assistant*. The score is the number of script tasks done. The set also
has requests to act (show the DEM, stones bigger than 10 cm, the DEM again at 2 cm, photos switched off by name, the
photos of the selected area, the orthophoto exported): each passes when the model calls the right action with those
parameters - the calls are recorded, nothing runs. Two chunks each with an orthophoto: with one of them shown, "find
the stones on the orthophoto" passes when the model draws them at once; with nothing shown, "draw the stones on the
orthophoto" passes when it asks which one and acts on none (`--only actions`).

```bash
ppg assistant eval FOLDER/scavo.ppg --kind local --base-url http://127.0.0.1:8080/v1 --out local.json --record
```

`--record` writes the score into the settings, where *Settings...* shows it as the last evaluation of the kind.

| Model | Where | Questions | Scripts (score) |
| --- | --- | --- | --- |
| Qwen2.5-Coder-7B-Instruct Q4_K_M | local, llama.cpp (`llama-server --jinja`), Apple M4 Pro | 3/8 in Italian, 2/8 in English | 3/6 in Italian, 3/6 in English |
| Qwen2.5-7B-Instruct Q4_K_M | local, the same | 2/8 in Italian, 0/8 in English | 1/6 in Italian, 0/6 in English |

The reference answers of the set (`--kind reference`) pass 8/8 and 6/6: every check can be met. Qwen2.5-Coder writes
the calls to the project's tools as text (the program reads them, see above) and does the simpler scripts - the region,
the normal map, the chunk of an area - but invents functions for the classification and the export. Qwen2.5-7B-Instruct
makes real calls but repeats them until its context is full, and is slower: among these two, Qwen2.5-Coder-7B is the one
to load. Both are small models: read their answers about the project with care. Claude and OpenAI were not evaluated
yet (no key on the computer of the evaluation).
