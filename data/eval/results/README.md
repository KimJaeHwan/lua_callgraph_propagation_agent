# Evaluation Result Storage Policy

This directory keeps lightweight, reviewable evaluation outputs in Git.

- `summaries/`: tracked compact summaries used for README/docs/history.
- `representative/`: tracked small representative result samples.
- root-level `*.json`: ignored full traces generated during local experiments.

Full result JSON files can become large because they include every candidate,
score component, and evidence string. Keep them locally for debugging, but do
not commit them unless there is a specific reason.
