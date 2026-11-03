# The evaluation set of the assistant

`src/pyarchinit_photogrammetry/assistant/evaluation/cases.json` (package data, so the installed program and the
application have it) is the set `ppg assistant eval` asks a model, on the demo project
(`ppg demo FOLDER --process`, then `FOLDER/scavo.ppg`).

* **questions** (8): each asked in a fresh conversation with the project open. It passes when every regular
  expression of `must_contain` is found in the answer (case-insensitive). `tools_expected` are the fact tools a good
  answer reads; those the model did not call are said in the detail but do not decide the result.
  `question_it`/`question_en` and the reference answers `example_it`/`example_en`.
* **scripts** (6): the model writes a script for `task_it`/`task_en`; it runs on a copy of the project (the project
  is never changed) and then `check`, a Python expression over `doc`, `chunk` (the chunk active before the script),
  `result` (the `ScriptResult`), `polygon`, `Path` and `np`, must be true. `polygon` (optional) is a box given as
  fractions of the extent of the DEM: `[[x0, y0], [x1, y1]]`. When the script fails, or runs and the check is false,
  the error goes back to the model once (as *Errore? Chiedi all'assistente* in the pane) and the corrected script runs
  on a fresh copy: attempt 2. `example` is a reference script.

The score of a model is the number of script tasks done within the two attempts; under 2 the model is "limited for
scripts" (shown in the settings of the assistant after `--record`).

```bash
ppg assistant eval FOLDER/scavo.ppg --kind reference            # the reference answers: every check can be met
ppg assistant eval FOLDER/scavo.ppg --kind local --base-url http://127.0.0.1:8080/v1 --out local.json --record
ppg assistant eval FOLDER/scavo.ppg --kind claude --only scripts --lang en
```

The checks are written for the demo: the markers are `target 0` to `target 7`, the dense cloud is not classified,
there is no region, no normal map and one chunk; with an untouched copy every check is false. When the demo changes,
run `--kind reference` again: all 14 cases must pass.
