import json
import subprocess  # nosec B404
import zipfile
from pathlib import Path

root = Path("/zdata/nclaude-installer")
(root / "config").mkdir(parents=True, exist_ok=True)
(root / "scripts").mkdir(parents=True, exist_ok=True)
(root / "tests").mkdir(parents=True, exist_ok=True)

models = [
    "nvidia/nvidia/nemotron-3.5-super-vl-120b-a12b",
    "nvidia/nvidia/nemotron-3.5-lightning-30b-a3b",
    "nvidia/nvidia/nemotron-3-ultra-550b-a55b",
    "nvidia/z-ai/glm-5.3-flash",
    "nvidia/deepseek-ai/deepseek-v4.1-flash",
    "nvidia/poolside/laguna-xs-2.1",
    "nvidia/minimaxai/minimax-m3.1-flash",
    "nvidia/stepfun-ai/step-5-preview",
    "nvidia/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
    "nvidia/google/gemma-4-31b-it",
    "nvidia/openai/gpt-oss-20b",
    "nvidia/meta/muse-glimmer-30b",
    "nvidia/meta/llama-3.2-90b-vision-instruct",
    "nvidia/meta/llama-3.2-11b-vision-instruct",
]

(root / "config/models.json").write_text(
    json.dumps(
        {
            "schema_version": 1,
            "provider": "NVIDIA NIM",
            "normalization": "Remove exactly one leading legacy 'nvidia/' prefix before passing the ID to NVIDIA's API.",
            "models": [{"rank": i + 1, "legacy_id": m} for i, m in enumerate(models)],
        },
        indent=2,
    )
    + "\n",
    encoding="utf-8",
)

# Read maintained source files from the repository instead of embedding legacy
# copies. This guarantees the generated release always matches the fixed,
# reviewed standalone implementation and can never reintroduce regressions.
installer_root = Path(__file__).resolve().parent / "nclaude-installer"


def _read(rel: str) -> str:
    return (installer_root / rel).read_text(encoding="utf-8")


(root / "scripts/nim_registry.py").write_text(
    _read("scripts/nim_registry.py"), encoding="utf-8"
)
(root / "install.sh").write_text(_read("install.sh"), encoding="utf-8")
(root / "install.sh").chmod(0o755)
(root / "uninstall.sh").write_text(_read("uninstall.sh"), encoding="utf-8")
(root / "uninstall.sh").chmod(0o755)
(root / "Makefile").write_text(_read("Makefile"), encoding="utf-8")
(root / "tests/test_registry.py").write_text(
    _read("tests/test_registry.py"), encoding="utf-8"
)
(root / "README.md").write_text(_read("README.md"), encoding="utf-8")
(root / ".gitignore").write_text(_read(".gitignore"), encoding="utf-8")
(root / "LICENSE").write_text(_read("LICENSE"), encoding="utf-8")

# Run static checks and tests before packaging.
checks = [
    ["bash", "-n", str(root / "install.sh")],
    ["bash", "-n", str(root / "uninstall.sh")],
    [
        "python3",
        "-m",
        "py_compile",
        str(root / "scripts/nim_registry.py"),
        str(root / "tests/test_registry.py"),
    ],
    ["python3", "-m", "json.tool", str(root / "config/models.json")],
    ["make", "-C", str(root), "all"],
]
for cmd in checks:
    proc = subprocess.run(cmd, check=False, text=True, capture_output=True)  # nosec B603
    if proc.returncode != 0:
        raise RuntimeError(
            f"Validation failed: {' '.join(cmd)}\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )

# Remove bytecode caches from release.
import shutil

for p in root.rglob("__pycache__"):
    shutil.rmtree(p)

zip_path = Path("/zdata/nclaude-installer.zip")
with zipfile.ZipFile(
    zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
) as zf:
    for p in sorted(root.rglob("*")):
        if p.is_file():
            zf.write(p, p.relative_to(root.parent))
print("Created:", zip_path)
print("Size:", zip_path.stat().st_size, "bytes")
print("Files:")
for p in sorted(root.rglob("*")):
    if p.is_file():
        print("-", p.relative_to(root))
