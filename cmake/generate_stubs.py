#!/usr/bin/env python3
"""Generate .pyi type stubs for the pyoomph._core extension module.

This reproduces what the old top-level build script did with
`pybind11-stubgen` + `src/nanobind/patch_stubs.py`, but is invoked as a
POST_BUILD step on the `_core` CMake target so it happens automatically as
part of `./configure && make` / `pip install .` via scikit-build-core.
Since the switch to nanobind, stub generation uses nanobind's own bundled
`python -m nanobind.stubgen` (always available - nanobind is a hard build
dependency, unlike the old optional `pybind11-stubgen`).

The stub is REQUIRED, not a convenience. It is what gives editors and type
checkers the API of the compiled extension, and the wheel also ships
`py.typed`, which tells them the information is there - a wheel without the
stub therefore promises typing and delivers an unintrospectable binary,
i.e. no completion at all rather than obviously none.

This script used to exit 0 unconditionally, on the reasoning that stubs are
a developer convenience and must never break a wheel build. That is exactly
how the Windows wheel shipped without a `.pyi` for as long as it did: the
extension failed to import under `nanobind.stubgen` (see
`cmake/stubgen_launcher.py`), the failure was printed and swallowed, and the
build stayed green. With `--required` (which CMake passes unless
PYOOMPH_REQUIRE_STUBS=OFF) any failure to produce the stub is fatal, so the
next such breakage stops the build that would have shipped it.

`--expect-file` guards against generating the stub from the wrong binary.
Stub generation imports `<module-name>`, and that import goes through the
normal `sys.path` machinery, which tries every suffix in
`importlib.machinery.EXTENSION_SUFFIXES` in order. A build directory still
holding an older `<module-name>.cpython-3xx-<plat>.so` beside the freshly
linked `<module-name>.abi3.so` therefore hands stubgen the stale one,
because the versioned suffix is tried first.

That is not hypothetical: a pre-nanobind pybind11 leftover shadowed the
real module for weeks. pybind11 wraps class methods in
`PyInstanceMethod_New`, a shape `nanobind.stubgen` does not recognise, so
every method degraded to `name: instancemethod = ...` and the only symptom
was `patch_stubs.py` raising on whichever of its patterns came first -
which points at a binding that never changed. CMake knows the real path as
`$<TARGET_FILE:_pyoomph_core>` and passes it here, so the mismatch is now
reported as itself, naming the file that has to go.

Usage (called from CMakeLists.txt):
    generate_stubs.py --module-dir DIR --module-name _core \
        --stage-dir DIR [--expect-file PATH] [--extra-copy-dir DIR] \
        [--patch-script PATH] [--python EXE]

On success, `<stage-dir>` ends up containing either:
  - `<module-name>.pyi`               (flat module, the common case), or
  - `<module-name>/__init__.pyi` (+.pyi siblings) (module with submodules)
so that `install(DIRECTORY "<stage-dir>/" DESTINATION pyoomph OPTIONAL)`
in CMakeLists.txt can drop it straight next to the built extension.

`--extra-copy-dir` additionally mirrors the same stub into a second
location - normally the source-tree `pyoomph/` package directory - so that
static analyzers (Pylance/Pyright/mypy) editing the checked-out source can
resolve `pyoomph._core` even without a full `pip install`, since they never
see the build/install directory.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def run(cmd, **kwargs):
    print("+ " + " ".join(str(c) for c in cmd), file=sys.stderr)
    return subprocess.run(cmd, **kwargs)


# Asked of the *target* interpreter rather than answered in this process: --python may be a
# different interpreter than the one running this script, and both EXTENSION_SUFFIXES and the
# sys.path it is resolved against belong to that one. find_spec() locates the module without
# importing it, so this stays cheap and cannot fail for load-time reasons (missing DLLs on
# Windows, say) that are not what is being asked about here.
_RESOLVE_SRC = (
    "import importlib.machinery as m, importlib.util as u, json, sys\n"
    "try:\n"
    "    spec = u.find_spec(sys.argv[1])\n"
    "except BaseException as e:\n"
    "    out = {'error': type(e).__name__ + ': ' + str(e)}\n"
    "else:\n"
    "    out = {'origin': None if spec is None else spec.origin}\n"
    "out['suffixes'] = list(m.EXTENSION_SUFFIXES)\n"
    "print(json.dumps(out))\n"
)


def same_file(a: str, b: str) -> bool:
    """Compare two paths the way the loader effectively does."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        # One of them does not exist (or is unstattable); fall back to a textual comparison
        # rather than reporting a spurious mismatch.
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def resolution_mismatch(python: str, module_name: str, expect_file: str,
                        module_dir: str, env) -> "str | None":
    """Return an error message if importing `module_name` would not load `expect_file`.

    Returns None when the resolution is correct (or could not be determined, which is
    reported by the caller as its own failure rather than silently accepted).
    """
    proc = subprocess.run([python, "-c", _RESOLVE_SRC, module_name],
                          env=env, stdout=subprocess.PIPE, text=True)
    if proc.returncode != 0 or not proc.stdout.strip():
        return (f"Could not determine which file {module_name!r} resolves to "
                f"(exit {proc.returncode}) - refusing to generate a stub that may describe "
                f"a different binary than {expect_file}")
    try:
        info = json.loads(proc.stdout.strip().splitlines()[-1])
    except ValueError:
        return (f"Unparseable resolution probe output for {module_name!r}: "
                f"{proc.stdout.strip()!r}")

    if "error" in info:
        return (f"Could not resolve {module_name!r}: {info['error']}")

    origin = info.get("origin")
    if origin and same_file(origin, expect_file):
        return None

    # A mismatch is nearly always an older artifact sitting next to the new one under a
    # suffix Python tries first, so name the actual files instead of only the two paths.
    lines = []
    if origin is None:
        lines.append(f"{module_name!r} is not importable from {module_dir}")
    else:
        lines.append(f"{module_name!r} resolves to")
        lines.append(f"    {origin}")
        lines.append("but the module this build just linked is")
        lines.append(f"    {expect_file}")
        lines.append("Stub generation imports the module, so the stub would describe the "
                     "wrong binary.")

    present = [Path(module_dir) / f"{module_name}{suffix}"
               for suffix in info.get("suffixes") or []]
    present = [c for c in present if c.exists()]
    if present:
        lines.append(f"Files in {module_dir} matching {module_name!r}, in the order Python "
                     f"tries them:")
        for cand in present:
            marker = "  <-- wins" if cand == present[0] else ""
            lines.append(f"    {cand.name}{marker}")
        lines.append("Delete the stale one(s) - they are build output, not sources - and "
                     "rebuild.")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--module-dir", required=True,
                         help="Directory containing the built extension (added to PYTHONPATH)")
    parser.add_argument("--module-name", default="_core",
                         help="Import name of the extension module (default: _core)")
    parser.add_argument("--stage-dir", required=True,
                         help="Directory the final stub(s) are normalized into")
    parser.add_argument("--expect-file", default=None,
                         help="Path of the extension that --module-name MUST resolve to "
                              "(CMake passes $<TARGET_FILE:...>). Guards against an older "
                              "build artifact with a higher-priority extension suffix "
                              "shadowing the freshly linked module.")
    parser.add_argument("--extra-copy-dir", action="append", default=[],
                         help="Additional directory (e.g. the source-tree pyoomph/ "
                              "package) to mirror the final stub(s) into, so editors "
                              "like Pylance can resolve pyoomph._core without needing "
                              "a full `pip install`. May be given multiple times.")
    parser.add_argument("--patch-script", default=None,
                         help="Optional patch_stubs.py-style script run on the generated stub")
    parser.add_argument("--python", default=sys.executable,
                         help="Python interpreter to use (default: this interpreter)")
    parser.add_argument("--required", action="store_true",
                         help="Fail (exit non-zero) if the stub cannot be generated, instead of "
                              "reporting it and exiting 0. Passed by CMake unless the build sets "
                              "PYOOMPH_REQUIRE_STUBS=OFF.")
    args = parser.parse_args()

    # Every "cannot produce a stub" path funnels through this, so that --required cannot be
    # honoured in some of them and forgotten in others.
    def give_up(message: str) -> int:
        print(message, file=sys.stderr)
        if args.required:
            print("Stub generation is REQUIRED for this build and it failed. Re-run the build "
                  "with -DPYOOMPH_REQUIRE_STUBS=OFF to downgrade this to a warning.",
                  file=sys.stderr)
            return 1
        return 0

    try:
        import nanobind.stubgen  # noqa: F401
    except ImportError:
        return give_up("nanobind.stubgen not importable - cannot generate the .pyi stub "
                       "for pyoomph._core")

    stage_dir = Path(args.stage_dir)
    stage_dir.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env["PYTHONPATH"] = args.module_dir + os.pathsep + env.get("PYTHONPATH", "")

    # Checked before stubgen runs, not after: once the wrong module has been imported the
    # only evidence left is a stub whose contents look merely unexpected, which is how this
    # went unnoticed the first time (see --expect-file in the module docstring).
    if args.expect_file:
        mismatch = resolution_mismatch(args.python, args.module_name, args.expect_file,
                                       args.module_dir, env)
        if mismatch:
            return give_up(mismatch)

    # nanobind.stubgen always writes a single flat "<module>.pyi" file (no
    # "<module>/__init__.pyi" package-directory form the way pybind11-stubgen
    # could produce for modules with submodules).
    # -P/--include-private: pyoomph's Python layer calls a number of leading-underscore
    # methods directly (e.g. _set_current_codegen, _resolve_based_on_domain_name), which
    # nanobind.stubgen omits by default (unlike the old pybind11-stubgen, which always
    # included them) - keep them in the stub so editors/type-checkers can resolve them.
    # Routed through stubgen_launcher.py rather than "-m nanobind.stubgen" directly: on Windows
    # the extension is imported straight out of the build tree, where its MSYS2/UCRT64
    # dependencies are only on PATH - which CPython >= 3.8 does not search for extension DLLs.
    # See the module docstring there. On the other platforms the launcher is a passthrough.
    launcher = Path(__file__).with_name("stubgen_launcher.py")
    if launcher.exists():
        base_cmd = [args.python, str(launcher),
                    "-m", args.module_name, "-O", str(stage_dir), "-P"]
    else:
        base_cmd = [args.python, "-m", "nanobind.stubgen",
                    "-m", args.module_name, "-O", str(stage_dir), "-P"]

    result = run(base_cmd, env=env)
    if result.returncode != 0:
        return give_up("Error in stub generation")

    flat_stub = stage_dir / f"{args.module_name}.pyi"
    if flat_stub.exists():
        target = flat_stub
    else:
        return give_up(f"nanobind.stubgen did not produce a stub for {args.module_name!r}")

    if args.patch_script:
        patch_script = Path(args.patch_script)
        if patch_script.exists():
            patch_result = run([args.python, str(patch_script), str(target)])
            if patch_result.returncode != 0:
                # patch_stubs.py raises when a pattern it is replacing is not in the stub, which
                # means the binding it corrects has changed shape - the stub is then wrong about
                # a nullable return or a numpy-accepting parameter rather than merely unpatched.
                return give_up("Error while patching the generated stub")
        else:
            return give_up(f"patch script {patch_script} not found")

    print(f"Generated stub: {target}")

    for extra_dir in args.extra_copy_dir:
        dest_root = Path(extra_dir)
        dest_root.mkdir(parents=True, exist_ok=True)
        if target.is_dir():
            dest = dest_root / target.name
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(target, dest)
        else:
            dest = dest_root / target.name
            shutil.copy2(target, dest)
        print(f"Mirrored stub into: {dest}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
