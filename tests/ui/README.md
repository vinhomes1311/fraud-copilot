# Dashboard rendering tests

Run from the repository root with Node.js 20 or later:

```bash
node --test tests/ui/risk-review.test.cjs
```

Uses Node's built-in runner, with no npm dependencies or external services.
Covers missing evidence, probability rounding, positive/negative effects,
HTML escaping, original brief access and unchanged stored evidence.
