# Reports

`ml-model-baseline.json` is a versioned reference required by the CI ML quality gate.
Other files in this directory are local generated output and are ignored by Git.

The pre-existing tracked model in `data/models/` remains available for serving
and the real-scoring CI smoke test. Ignore rules prevent adding new local artifacts;
they do not remove files already tracked by Git.
