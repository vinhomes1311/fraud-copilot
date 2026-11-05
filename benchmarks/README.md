# Benchmarks

- `compare_llm.py`: offline plan, mock runs and opt-in live comparisons.
- `playbook_eval.py`: validate frozen references and score generated reports.
- `compare_playbook_runs.py` / `run_playbook_v1_control.py`: reproduce historical comparisons.
- `fixtures/`: versioned synthetic cases and the historical control playbook.
- `config/`: versioned benchmark architecture and limits.

Usage: [benchmark guide](../docs/benchmarks/README.md).
Run commands from the repository root, for example:

```bash
python -m benchmarks.compare_llm
```

The default command only prints a plan. Generated results belong in
`benchmark_results/`, which is ignored by Git.
