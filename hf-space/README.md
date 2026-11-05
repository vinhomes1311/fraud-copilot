---
title: Fraud Copilot
emoji: "🛡️"
colorFrom: blue
colorTo: indigo
sdk: static
pinned: false
---

# Fraud Copilot - public demo

This static Space is an interactive presentation demo. It uses synthetic sample
transactions and illustrative risk scores in the browser. It does not call the
project's API, database, queue, or machine-learning model.

## Publish this demo

From the repository root, authenticate with Hugging Face and upload this folder
to the existing Space:

```powershell
hf auth login
hf upload vinhhomes1311/fraud-copilot .\hf-space . --repo-type space
```

The Space is public, so everything uploaded here is visible to anyone. Do not
put credentials, private datasets, or personal information in this folder.
