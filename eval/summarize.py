from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[1]
METRICS = (
    ("RCA correct", "rca_correct"),
    ("Action correct", "action_correct"),
    ("Final state correct", "final_state_correct"),
    ("Hallucinated claims", "hallucinated_claims"),
    ("Time to final state (s)", "time_to_final_state_seconds"),
    ("LLM calls", "llm_calls"),
    ("Prompt tokens", "llm_prompt_tokens"),
    ("Completion tokens", "llm_completion_tokens"),
    ("False resolutions", "false_resolution"),
)


def _number(value: str | None) -> float | None:
    if value in (None, ""):
        return None
    if value.lower() == "true":
        return 1.0
    if value.lower() == "false":
        return 0.0
    try:
        return float(value)
    except ValueError:
        return None


def _average(rows: list[dict[str, str]], field: str) -> str:
    values = [_number(row.get(field)) for row in rows]
    present = [value for value in values if value is not None]
    if not present:
        return "n/a"
    if field in {"rca_correct", "action_correct", "final_state_correct", "false_resolution"}:
        return f"{sum(present) / len(present) * 100:.1f}%"
    if field == "hallucinated_claims":
        return f"{mean(present):.2f}"
    return f"{mean(present):.1f}"


def render(results: list[dict[str, str]]) -> str:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in results:
        grouped[row.get("config", "unknown")].append(row)
    sections = [
        "# Evaluation summary",
        "",
        "LLM calls count successful model responses, including investigation/tool turns "
        "and RCA or validation retries; Kubernetes tool invocations are not LLM calls.",
    ]
    for config in ("full", "no_tools", "no_verification"):
        all_rows = grouped.get(config, [])
        rows = [row for row in all_rows if row.get("status") == "VALID"]
        invalid_count = sum(row.get("status") == "INVALID" for row in all_rows)
        sections.extend(
            [
                "",
                f"## {config} ({len(rows)} valid, {invalid_count} invalid)",
                "",
                "| Metric | Mean / rate |",
                "|---|---:|",
                f"| Invalid runs | {invalid_count} |",
            ]
        )
        sections.extend(
            f"| {label} | {_average(rows, field)} |" for label, field in METRICS
        )
    return "\n".join(sections) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize evaluation CSV results.")
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=ROOT / "eval" / "results",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    csv_files = sorted(args.results_dir.glob("*.csv"))
    rows: list[dict[str, str]] = []
    for path in csv_files:
        with path.open(newline="", encoding="utf-8") as file:
            rows.extend(csv.DictReader(file))
    if not rows:
        parser.error(f"No evaluation CSV files found in {args.results_dir}")
    summary = render(rows)
    output = args.output or args.results_dir / "summary.md"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(summary, encoding="utf-8")
    print(summary, end="")
    print(f"Saved: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
