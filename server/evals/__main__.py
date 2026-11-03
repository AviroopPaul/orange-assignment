"""`python -m evals`: run the scenarios, check the invariants, write the table.

The exit code is the result. A crash in one scenario is an ERROR row, never a missing one.
"""

import argparse
import asyncio
import logging
import socket
import sys
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path

from evals import report, scenarios  # noqa: F401
from evals.invariants import check_all
from evals.report import ERROR, FAIL, PASS, Row
from evals.scenario import REGISTRY, Eval, Scenario
from evals.stack import Stack

RESULTS_DIR = Path("/srv/results")
SCENARIO_LIMIT_S = 120
NO_ROUTE_HOST = "generativelanguage.googleapis.com"


def send_logs_to_file(results_dir: Path) -> None:
    """Expected failures log tracebacks on purpose. They belong in a file, not in the summary."""
    (results_dir / "logs").mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.WARNING, filename=results_dir / "logs" / "api.log", force=True
    )


def internet_is_reachable() -> bool:
    try:
        with socket.create_connection((NO_ROUTE_HOST, 443), timeout=3):
            return True
    except OSError:
        return False


def _actual(run: Eval) -> str:
    return "; ".join(f"{fact.name}={fact.actual}" for fact in run.facts) or "no facts recorded"


async def run_one(stack: Stack, item: Scenario) -> Row:
    started = time.monotonic()
    await stack.reset()
    run = None
    error = None
    invariants = "not checked"
    violations: dict[str, list[str]] = {}
    try:
        run = Eval(stack, item)
        async with asyncio.timeout(SCENARIO_LIMIT_S):
            await item.run(run)
        stack.pg_proxy.up()
        stack.redis_proxy.up()
        if not await stack.settle():
            run.note("Work was still open when the wait ended.")
        snapshot = await stack.snapshot(tuple(run.forbidden))
        violations = {name: found for name, found in check_all(snapshot).items() if found}
        invariants = (
            "; ".join(f"{name}: {found[0]}" for name, found in violations.items()) or "all held"
        )
    except Exception as caught:
        error = caught
        logging.error("%s crashed:\n%s", item.id, traceback.format_exc())

    facts = run.facts if run else []
    failed_facts = [fact for fact in facts if not fact.passed]
    notes = list(run.notes) if run else []
    if failed_facts:
        notes += [f"{fact.name} was {fact.actual}, wanted {fact.want}" for fact in failed_facts]
    if error is not None:
        notes.append(f"{type(error).__name__}: {str(error)[:200]}")
    if error is not None:
        result = ERROR
    elif failed_facts or violations or not facts:
        result = FAIL
    else:
        result = PASS
    return Row(
        id=item.id,
        category=item.category,
        scenario=item.title,
        expected=item.expected,
        actual=_actual(run) if run else "did not start",
        result=result,
        invariants=invariants,
        seconds=round(time.monotonic() - started, 1),
        notes=" ".join(notes),
    )


async def main(arguments: argparse.Namespace) -> int:
    results_dir = Path(arguments.results) if arguments.results else RESULTS_DIR
    send_logs_to_file(results_dir)
    if not arguments.allow_network and internet_is_reachable():
        print(f"{NO_ROUTE_HOST} can be reached, so this run is not offline. Stopping.")
        return 2

    chosen = arguments.only.split(",") if arguments.only else None
    known = {item.id for item in REGISTRY}
    unknown = sorted(set(chosen or ()) - known)
    if unknown:
        print(f"Unknown scenarios: {unknown}")
        return 2
    items = [item for item in REGISTRY if chosen is None or item.id in chosen]
    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    stack = Stack(results_dir)
    await stack.start()
    rows: list[Row] = []
    try:
        for item in items:
            row = await run_one(stack, item)
            rows.append(row)
            print(f"{row.result:5} {row.id:5} {row.seconds:6}s  {row.scenario}")
            if row.result != PASS:
                print(f"      {row.notes} [{row.invariants}]")
    finally:
        await stack.stop()

    report.write_all(results_dir, rows, started_at, chosen is not None)
    totals = report.summary(rows)
    print(f"{totals['passed']} of {totals['total']} passed. Results are in {results_dir}.")
    missing = report.uncovered_categories(rows)
    if chosen is None and missing:
        print(f"No scenario covers: {missing}")
        return 1
    return 0 if totals["passed"] == totals["total"] and totals["total"] > 0 else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", help="Comma-separated scenario IDs")
    parser.add_argument("--results", help="Where to write the table")
    parser.add_argument("--allow-network", action="store_true", help="Skip the offline check")
    sys.exit(asyncio.run(main(parser.parse_args())))
