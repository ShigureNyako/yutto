from __future__ import annotations

import importlib
import os
import runpy
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.processor

ROOT = Path(__file__).resolve().parents[2]
HELPER = runpy.run_path(str(ROOT / "scripts/prepare-ci-cache.py"))
fingerprint = HELPER["fingerprint"]
prepare_metadata = HELPER["prepare_metadata"]


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    return tmp_path


@pytest.mark.parametrize(
    "project,variable",
    [
        ("pyproject.toml", "YUTTO_CI_WHEEL_KEY"),
        ("packages/biliass/pyproject.toml", "BILIASS_CI_WHEEL_KEY"),
    ],
)
def test_ci_overlay_preserves_metadata_and_environment_keys(project: str, variable: str):
    # In CI the working copy is already overlaid; exercise the committed inputs.
    original = subprocess.check_output(["git", "show", f"HEAD:{project}"], cwd=ROOT, text=True)
    expected = tomllib.loads(original)
    keys = expected["tool"]["uv"]["cache-keys"]
    expected["tool"]["uv"]["cache-keys"] = [{"env": variable}, *(key for key in keys if "env" in key)]
    prepared = prepare_metadata(original, variable)
    assert tomllib.loads(prepared) == expected
    with pytest.raises(ValueError, match="already been prepared"):
        prepare_metadata(prepared, variable)


def test_unknown_metadata_layout_fails_closed():
    with pytest.raises(ValueError, match="Expected to change only"):
        prepare_metadata('[tool.uv]\ncache-keys = [{ file = "pyproject.toml" }]\n', "YUTTO_CI_WHEEL_KEY")


@pytest.mark.parametrize(
    "name",
    [
        "src/yutto/module.py",
        "src/yutto/_core.pyi",
        "rust/crates/yutto/src/lib.rs",
        "rust/Cargo.toml",
        "rust/Cargo.lock",
        "packages/biliass/rust/build.rs",
        "packages/biliass/rust/proto/danmaku.proto",
        "packages/biliass/pyproject.toml",
        "pyproject.toml",
        "uv.lock",
        "README.md",
        "LICENSE",
    ],
)
def test_content_changes_invalidate_but_checkout_timestamps_do_not(checkout: Path, name: str):
    path = checkout / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("original")
    package = "BILIASS_CI_WHEEL_KEY" if name.startswith("packages/biliass/") else "YUTTO_CI_WHEEL_KEY"
    inputs = [*HELPER["COMMON_INPUTS"], *HELPER["PACKAGES"][package][1]]
    before = fingerprint(checkout, inputs, {"abi": "cp314"})
    os.utime(path, (1, 1))
    assert fingerprint(checkout, inputs, {"abi": "cp314"}) == before
    path.write_text("changed")
    assert fingerprint(checkout, inputs, {"abi": "cp314"}) != before
    path.unlink()
    assert fingerprint(checkout, inputs, {"abi": "cp314"}) != before


def test_input_paths_and_modes_contribute_to_identity(checkout: Path):
    path = checkout / "old.py"
    path.write_text("same content")
    before = fingerprint(checkout, ["."], {})
    path.chmod(0o755)
    assert fingerprint(checkout, ["."], {}) != before
    before = fingerprint(checkout, ["."], {})
    path.rename(checkout / "new.py")
    assert fingerprint(checkout, ["."], {}) != before


@pytest.mark.parametrize(
    "field",
    ["python", "implementation", "abi", "gil-disabled", "platform", "machine", "image", "rustc", "cc", "checkout"],
)
def test_build_environment_changes_invalidate(checkout: Path, field: str):
    assert fingerprint(checkout, ["."], {field: "before"}) != fingerprint(checkout, ["."], {field: "after"})


@pytest.mark.parametrize("variable", ["RUSTFLAGS", "CARGO_BUILD_TARGET", "PYO3_CONFIG_FILE", "MATURIN_PEP517_ARGS"])
def test_build_environment_collects_native_options(checkout: Path, monkeypatch: pytest.MonkeyPatch, variable: str):
    monkeypatch.setenv(variable, "changed-option")
    check_output = subprocess.check_output
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda command, **kwargs: "test compiler" if command[0] in {"rustc", "cc"} else check_output(command, **kwargs),
    )
    environment = HELPER["build_environment"](checkout)
    assert environment[variable] == "changed-option"
    assert environment["python"] == sys.version
    assert environment["implementation"] == sys.implementation.name


def test_overlay_cannot_run_accidentally_outside_ci(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with pytest.raises(RuntimeError, match="only for disposable GitHub Actions"):
        HELPER["main"]()


@pytest.mark.skipif(os.environ.get("UV_NO_EDITABLE") != "true", reason="CI non-editable installation check")
def test_ci_wheels_contain_the_checked_out_python_sources():
    for package, source in [("yutto", ROOT / "src/yutto"), ("biliass", ROOT / "packages/biliass/src/biliass")]:
        module = importlib.import_module(package)
        native = importlib.import_module(f"{package}._core")
        assert module.__file__ is not None and native.__file__ is not None
        installed = Path(module.__file__).resolve().parent
        assert installed.is_relative_to(Path(sys.prefix).resolve())
        assert Path(native.__file__).resolve().is_relative_to(installed)
        for path in source.rglob("*"):
            if path.is_file() and (path.suffix in {".py", ".pyi"} or path.name == "py.typed"):
                assert (installed / path.relative_to(source)).read_bytes() == path.read_bytes()
