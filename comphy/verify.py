#!/usr/bin/env python3
"""Run the bounded CoMPhy regressions against one installed candidate wheel."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone


def _run(command: list[str], *, cwd: Path, timeout: int) -> dict:
    started = time.monotonic()
    process = subprocess.run(
        command,
        cwd=cwd,
        env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return {
        "command": command,
        "exit_code": process.returncode,
        "duration_seconds": round(time.monotonic() - started, 3),
        "stdout": process.stdout,
        "stderr": process.stderr,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _git_head(source: Path) -> str:
    process = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return process.stdout.strip()


def _write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", required=True, type=Path, help="candidate wheel built from --source")
    parser.add_argument("--candidate-sha", help="expected full candidate Git commit")
    parser.add_argument("--source", required=True, type=Path, help="candidate pyoomph checkout")
    parser.add_argument("--report", required=True, type=Path, help="persistent JSON receipt")
    args = parser.parse_args()

    control_root = Path(__file__).resolve().parent.parent
    source = args.source.resolve()
    wheel = args.wheel.resolve()
    report_path = args.report.resolve()
    started_at = datetime.now(timezone.utc)
    report = {
        "schema": "comphy-pyoomph-verification-v1",
        "started_at": started_at.isoformat(),
        "candidate_sha": args.candidate_sha,
        "source": str(source),
        "wheel": str(wheel),
        "status": "failed",
        "stages": [],
        "cleanup": {"complete": False},
    }
    work_path: Path | None = None

    try:
        if not wheel.is_file():
            raise RuntimeError(f"candidate wheel does not exist: {wheel}")
        if not (source / "tests" / "test_state_file_restart.py").is_file():
            raise RuntimeError(f"--source is not a pyoomph checkout: {source}")
        actual_sha = _git_head(source)
        report["source_sha"] = actual_sha
        if args.candidate_sha and actual_sha != args.candidate_sha:
            raise RuntimeError(
                f"candidate SHA mismatch: --candidate-sha={args.candidate_sha}, source HEAD={actual_sha}"
            )
        report["candidate_sha"] = actual_sha
        report["wheel_sha256"] = _sha256(wheel)

        report_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".comphy-pyoomph-", dir=report_path.parent) as raw_work:
            work = Path(raw_work)
            work_path = work
            environment = work / "venv"
            create = _run([sys.executable, "-m", "venv", str(environment)], cwd=work, timeout=120)
            report["stages"].append({"name": "create_environment", **create})
            if create["exit_code"]:
                raise RuntimeError("could not create the isolated test environment")

            python = environment / "bin" / "python"
            install = _run(
                [str(python), "-I", "-m", "pip", "install", str(wheel), "pytest>=8,<9", "mpi4py>=4,<5"],
                cwd=work,
                timeout=600,
            )
            report["stages"].append({"name": "install_candidate", **install})
            if install["exit_code"]:
                raise RuntimeError("candidate wheel or its test dependencies did not install")

            doctor_code = (
                "import json, pathlib; import pyoomph; "
                "from importlib.metadata import version; from pyoomph.generic.mpi import has_mpi; "
                "print(json.dumps({'import_path':str(pathlib.Path(pyoomph.__file__).resolve()),"
                "'version':version('pyoomph'),'has_mpi':has_mpi()}))"
            )
            doctor = _run([str(python), "-I", "-c", doctor_code], cwd=work, timeout=60)
            report["stages"].append({"name": "doctor", **doctor})
            if doctor["exit_code"]:
                raise RuntimeError("installed candidate could not be imported")
            metadata = json.loads(doctor["stdout"].splitlines()[-1])
            imported = Path(metadata["import_path"])
            if imported.is_relative_to(source) or imported.is_relative_to(control_root):
                raise RuntimeError(f"source-shadowed import rejected: {imported}")
            if not metadata["has_mpi"]:
                raise RuntimeError("candidate wheel was not compiled with MPI support")
            report["installed_candidate"] = metadata

            staged = work / "tests"
            staged.mkdir()
            for name in (
                "test_remeshing_rss.py",
                "taylor_culick_case.py",
                "test_taylor_culick_restart.py",
            ):
                shutil.copy2(control_root / "comphy" / "tests" / name, staged / name)
            for name in ("test_state_file_restart.py", "test_remeshing_leaks.py"):
                shutil.copy2(source / "tests" / name, staged / name)

            selections = [
                "tests/test_remeshing_rss.py",
                "tests/test_remeshing_leaks.py::test_remeshed_problem_is_not_leaked[via_recreation]",
                "tests/test_state_file_restart.py::test_restart_reproduces_the_state_and_the_continuation[superlu-0.0-transient]",
                "tests/test_taylor_culick_restart.py",
            ]
            tests = _run(
                [str(python), "-I", "-m", "pytest", "-q", "-ra", "-s", "--durations=10", *selections],
                cwd=work,
                timeout=600,
            )
            report["stages"].append({"name": "regressions", **tests})
            if tests["exit_code"]:
                raise RuntimeError("one or more CoMPhy regressions failed")
            if " passed" not in tests["stdout"] or " skipped" in tests["stdout"]:
                raise RuntimeError("regression selection did not produce an unskipped passing suite")

        report["cleanup"] = {"complete": not Path(raw_work).exists(), "path": raw_work}
        if not report["cleanup"]["complete"]:
            raise RuntimeError(f"run-owned directory remains after cleanup: {raw_work}")
        report["status"] = "passed"
        return_code = 0
    except (Exception, subprocess.SubprocessError) as error:
        report["error"] = f"{type(error).__name__}: {error}"
        return_code = 1

    if work_path is None:
        report["cleanup"] = {"complete": True, "path": None, "note": "run directory was not created"}
    else:
        report["cleanup"] = {"complete": not work_path.exists(), "path": str(work_path)}
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["duration_seconds"] = round((datetime.now(timezone.utc) - started_at).total_seconds(), 3)
    _write_report(report_path, report)
    print(json.dumps({
        "status": report["status"],
        "candidate_sha": report.get("candidate_sha"),
        "report": str(report_path),
        "error": report.get("error"),
    }))
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
