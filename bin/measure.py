"""Record real stage durations, including failed commands, without caching output."""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import time


def record(stage, seconds, outcome, **details):
    entry = dict(stage=stage, seconds=round(seconds, 3), outcome=outcome, **details)
    print("HOSTINGER_TIMING " + json.dumps(entry), flush=True)
    folder = Path("deploy-diagnostics")
    folder.mkdir(exist_ok=True)
    job = os.environ.get("HOSTINGER_TIMING_JOB", "local")
    with (folder / f"timings-{job}.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry) + "\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write(f"- `{stage}`: {entry['seconds']:.3f}s, {outcome}"
                         + (f", download cache hit: {details['cache_hit']}" if "cache_hit" in details else "") + "\n")


@contextmanager
def timed(stage, **details):
    started = time.monotonic()
    outcome = "failure"
    try:
        yield
        outcome = "success"
    finally:
        record(stage, time.monotonic() - started, outcome, **details)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("stage")
    parser.add_argument("--cache-hit", default="unknown")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        parser.error("A command is required")
    try:
        with timed(args.stage, cache_hit=args.cache_hit or "false"):
            subprocess.run(command, check=True)
    except subprocess.CalledProcessError as error:
        raise SystemExit(error.returncode)


if __name__ == "__main__":
    main()
