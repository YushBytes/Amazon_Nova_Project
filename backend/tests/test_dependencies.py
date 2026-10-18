"""Smoke tests for Phase 2 defect D: incomplete dependency manifest.

Audit reference: docs/PROJECT_AUDIT.md section 3 ("Missing from requirements.txt").

`requirements.txt` declared 8 packages but omitted boto3, ffmpeg-python, Pillow
and python-multipart. Because app.py imports projects.py, which imports boto3, a
clean `pip install -r requirements.txt` followed by `uvicorn app:app` failed with
ModuleNotFoundError. These tests fail if that regresses.

They deliberately assert on *importability and declaration*, not on pinned
version numbers, so routine dependency bumps do not break the suite.
"""

import importlib
import re
from pathlib import Path

import pytest

REQUIREMENTS = Path(__file__).resolve().parent.parent / "requirements.txt"

# Package name as written in requirements.txt -> module name it provides.
# Only direct imports made by backend source are listed.
DIRECT_DEPENDENCIES = {
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "pydantic": "pydantic",
    "python-jose": "jose",
    "boto3": "boto3",
    "motor": "motor",
    "pymongo": "pymongo",
    "ffmpeg-python": "ffmpeg",
    "Pillow": "PIL",
    "python-dotenv": "dotenv",
}

# Provided transitively by pymongo rather than declared separately.
PYMONGO_PROVIDED = ["gridfs", "bson"]


def _declared_packages():
    """Package names declared in requirements.txt, lowercased, extras stripped."""
    names = set()
    for raw in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # e.g. "python-jose[cryptography]==3.5.0" -> "python-jose"
        name = re.split(r"[\[=<>!~;]", line, maxsplit=1)[0].strip()
        if name:
            names.add(name.lower())
    return names


# --------------------------------------------------------------------------
# declaration
# --------------------------------------------------------------------------

@pytest.mark.parametrize("package", sorted(DIRECT_DEPENDENCIES))
def test_direct_dependency_is_declared(package):
    """Every package the backend imports directly must be in requirements.txt."""
    assert package.lower() in _declared_packages(), (
        f"{package} is imported by backend source but not declared in "
        f"requirements.txt; a clean install would fail at import time"
    )


def test_multipart_declared_for_file_uploads():
    """FastAPI needs python-multipart to parse UploadFile form data.

    app.py's /upload-reel uses `UploadFile = File(...)`. Without this package
    the route raises at request time even though the module imports fine, so it
    cannot be caught by an import check alone.
    """
    assert "python-multipart" in _declared_packages()


def test_every_requirement_is_version_pinned():
    """Direct dependencies are pinned so builds are reproducible."""
    unpinned = []
    for raw in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "==" not in line:
            unpinned.append(line)
    assert not unpinned, f"unpinned requirements: {unpinned}"


# --------------------------------------------------------------------------
# importability
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "module", sorted(set(DIRECT_DEPENDENCIES.values()) | set(PYMONGO_PROVIDED))
)
def test_dependency_module_imports(module):
    """The declared packages actually provide the modules the backend imports."""
    importlib.import_module(module)


@pytest.mark.parametrize(
    "module",
    [
        "analyzer",
        "app",
        "auth",
        "aws_config",
        "db",
        "extractor",
        "orchestrator",
        "projects",
        "prompts",
        "s3_trigger",
        "safety",
        "video_stitcher",
        "audit_manager",
        "verifier",
    ],
)
def test_backend_module_imports(module):
    """Every backend module must import without optional-dependency errors.

    This is the check that would have caught defect D: importing `app` pulls in
    `projects`, which imports boto3.
    """
    importlib.import_module(module)


def test_app_registers_expected_routes():
    """Route registration must survive the dependency changes."""
    import app as app_module

    paths = set(app_module.app.openapi()["paths"])
    expected = {
        "/status",
        "/upload-reel",
        "/analyze/{video_id}",
        "/violations/{video_id}",
        "/submissions",
        "/projects/",
        "/projects/marketplace",
        "/projects/{project_id}/accept",
        "/projects/{project_id}/upload-init",
        "/projects/{project_id}/review",
        "/projects/{project_id}/chat",
    }
    missing = expected - paths
    assert not missing, f"routes missing from the app: {sorted(missing)}"


# --------------------------------------------------------------------------
# the FFmpeg executable is a separate, non-pip prerequisite
# --------------------------------------------------------------------------

def test_ffmpeg_python_is_only_a_wrapper():
    """Document that ffmpeg-python is not the FFmpeg executable.

    The pip package provides the `ffmpeg` module used by extractor.py, but the
    actual frame extraction shells out to the `ffmpeg` binary, which pip cannot
    install. The Dockerfile installs it with apt-get; local developers must
    install it themselves.
    """
    import ffmpeg

    assert hasattr(ffmpeg, "probe")
    assert hasattr(ffmpeg, "input")


@pytest.mark.requires_ffmpeg
def test_ffmpeg_executable_is_available():
    """Only meaningful where the FFmpeg binary is installed.

    Run with: pytest -m requires_ffmpeg
    Skipped by default because the executable is an environmental prerequisite
    rather than something the manifest can guarantee.
    """
    import shutil
    import subprocess

    exe = shutil.which("ffmpeg")
    if exe is None:
        pytest.skip("ffmpeg executable not on PATH")

    out = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0
    assert "ffmpeg version" in out.stdout.lower()
