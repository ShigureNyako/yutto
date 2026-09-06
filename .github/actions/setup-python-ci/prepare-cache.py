from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import sysconfig
import tomllib
from pathlib import Path

# Include packaging inputs as well as native sources: non-editable wheels also
# contain Python files. Keep tests/docs out so their changes can reuse wheels.
COMMON_INPUTS = [
    "pyproject.toml",
    "uv.lock",
    "justfile",
    ".cargo",
    "rust-toolchain",
    "rust-toolchain.toml",
    ".github/actions/setup-python-ci",
    ".github/workflows/unit-test.yml",
    ".github/workflows/e2e-test.yml",
    ".github/workflows/lint-and-fmt.yml",
]
PACKAGES = {
    "YUTTO_CI_WHEEL_KEY": ("pyproject.toml", ["src/yutto", "rust", "README.md", "LICENSE"]),
    "BILIASS_CI_WHEEL_KEY": ("packages/biliass/pyproject.toml", ["packages/biliass"]),
}
BUILD_ENV_PREFIXES = ("CARGO", "RUST", "PYO3", "PYTHON", "MATURIN", "CC", "CXX", "CFLAGS", "CPPFLAGS", "LDFLAGS")


def build_environment(root: Path) -> dict[str, str]:
    return {
        "checkout": str(root.resolve()),
        "python": sys.version,
        "implementation": sys.implementation.name,
        "interpreter": str(Path(sys.executable).resolve()),
        "abi": str(sysconfig.get_config_var("SOABI")),
        "gil-disabled": str(sysconfig.get_config_var("Py_GIL_DISABLED")),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "image": os.environ.get("ImageVersion", ""),
        "rustc": subprocess.check_output(["rustc", "-vV"], text=True),
        "cc": subprocess.check_output(["cc", "--version"], text=True),
        **{key: value for key, value in os.environ.items() if key.startswith(BUILD_ENV_PREFIXES)},
    }


def fingerprint(root: Path, inputs: list[str], environment: dict[str, str]) -> str:
    paths = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", *inputs], cwd=root
    )
    files = []
    for name in sorted(set(paths.split(b"\0")) - {b""}):
        path = root / os.fsdecode(name)
        # A new submodule in build inputs needs explicit content handling rather
        # than silently omitting it from the wheel identity.
        if not path.is_file():
            raise ValueError(f"Build input is not a file: {path}")
        files.append((os.fsdecode(name), path.stat().st_mode, hashlib.sha256(path.read_bytes()).hexdigest()))
    return hashlib.sha256(json.dumps([environment, files], sort_keys=True).encode()).hexdigest()


def prepare_metadata(text: str, variable: str) -> str:
    expected = tomllib.loads(text)
    keys = expected["tool"]["uv"]["cache-keys"]
    if any(key.get("env") in PACKAGES for key in keys):
        raise ValueError("CI cache metadata has already been prepared")
    # Retain explicit env keys so a later uv invocation still detects changed
    # MATURIN_PEP517_ARGS/RUSTFLAGS. Replace only checkout timestamp keys.
    env_keys = [{"env": variable}, *(key for key in keys if "env" in key)]
    replacement = "cache-keys = [\n" + "".join(f"  {{ env = {json.dumps(key['env'])} }},\n" for key in env_keys) + "]"
    prepared, count = re.subn(r"(?ms)^cache-keys = \[\n.*?^\]", replacement, text)
    expected["tool"]["uv"]["cache-keys"] = env_keys
    if count != 1 or tomllib.loads(prepared) != expected:
        raise ValueError("Expected to change only tool.uv.cache-keys")
    return prepared


def main() -> None:
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("This metadata overlay is only for disposable GitHub Actions checkouts")
    root = Path(os.environ["GITHUB_WORKSPACE"])
    # Match rust-cache's setting even when a wheel hit skips that action.
    os.environ["CARGO_INCREMENTAL"] = "0"
    environment = build_environment(root)
    keys = {}
    metadata = {}
    # Hash original files before applying either overlay. Never replace the
    # developer-facing timestamp keys in the committed pyproject files.
    for variable, (project, inputs) in PACKAGES.items():
        keys[variable] = fingerprint(root, [*COMMON_INPUTS, *inputs], environment)
        metadata[root / project] = prepare_metadata((root / project).read_text(), variable)
    for path, text in metadata.items():
        path.write_text(text)
    build_key = hashlib.sha256(json.dumps(environment, sort_keys=True).encode()).hexdigest()
    with Path(os.environ["GITHUB_ENV"]).open("a") as output:
        output.write(
            f"UV_NO_EDITABLE=true\nUV_PYTHON={environment['interpreter']}\n"
            f"CARGO_INCREMENTAL=0\nYUTTO_CI_BUILD_ENV_KEY={build_key}\n"
        )
        for variable, key in keys.items():
            output.write(f"{variable}={key}\n")
    combined = hashlib.sha256(json.dumps(keys, sort_keys=True).encode()).hexdigest()
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        output.write(f"cache-key={combined}\n")


if __name__ == "__main__":
    main()
