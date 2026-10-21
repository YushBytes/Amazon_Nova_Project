"""Smoke tests for Phase 2 defect D: incomplete dependency manifest.

Audit reference: docs/PROJECT_AUDIT.md section 3 ("Missing from requirements.txt").

`requirements.txt` declared 8 packages but omitted boto3, ffmpeg-python, Pillow
and python-multipart. Because app.py imports projects.py, which imports boto3, a
clean `pip install -r requirements.txt` followed by `uvicorn app:app` failed with
ModuleNotFoundError. These tests fail if that regresses.

They deliberately assert on *importability and declaration*, not on pinned
version numbers, so routine dependency bumps do not break the suite.

Phase 5 removed the orphaned MongoDB/GridFS pipeline, which was the only
importer of Pillow, gridfs and bson, so those are no longer expected. The
dead-module guards at the end of this file keep them from coming back
unnoticed.
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
    "python-dotenv": "dotenv",
}


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

@pytest.mark.parametrize("module", sorted(set(DIRECT_DEPENDENCIES.values())))
def test_dependency_module_imports(module):
    """The declared packages actually provide the modules the backend imports."""
    importlib.import_module(module)


@pytest.mark.parametrize(
    "module",
    [
        "analyzer",
        "app",
        "auth",
        "aws_resources",
        "config",
        "db",
        "detections",
        "extractor",
        "image_provider",
        "job_state",
        "orchestrator",
        "ownership",
        "pipeline",
        "projects",
        "prompts",
        "recover_jobs",
        "remediation",
        "remediation_repo",
        "remediation_routes",
        "remediation_state",
        "s3_trigger",
        "safety",
        "source_store",
        "video_repo",
        "video_stitcher",
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
        # Phase 8.
        "/remediations",
        "/remediations/capabilities",
        "/remediations/{remediation_id}",
        "/remediations/{remediation_id}/approve",
        "/remediations/{remediation_id}/reject",
        "/remediations/{remediation_id}/cancel",
        "/remediations/{remediation_id}/execute",
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


# --------------------------------------------------------------------------
# Phase 5: the removed legacy paths must stay removed
# --------------------------------------------------------------------------

BACKEND_DIR = Path(__file__).resolve().parent.parent

# Each entry is (module file, why it was removed). Reintroducing any of these
# would restore a second, incompatible storage pipeline or a mock that reports
# success without doing the work - both explicitly forbidden by CLAUDE.md.
REMOVED_MODULES = {
    "audit_manager.py": (
        "orphaned MongoDB/GridFS pipeline; read meta['gridfs_id'] that nothing "
        "ever wrote, and remediate_frame returned its input while the caller "
        "recorded remediated=True"
    ),
    "verifier.py": "GridFS frame-dump dev utility with a hardcoded connection string",
    "check_count.py": "throwaway debug script hardcoded to video_id='test_8k_v2'",
    "debug_probe.py": "throwaway FFmpeg probe debug script",
    "aws_config.py": "never imported; built an unused Step Functions client at import time",
}


@pytest.mark.parametrize("filename", sorted(REMOVED_MODULES))
def test_removed_module_is_absent(filename):
    """A deleted legacy module must not reappear."""
    path = BACKEND_DIR / filename
    assert not path.exists(), (
        f"{filename} was removed in Phase 5 ({REMOVED_MODULES[filename]}); "
        f"it is back at {path}. If it is genuinely needed again, that is a "
        f"decision to record, not a file to restore quietly."
    )


@pytest.mark.parametrize("filename", sorted(REMOVED_MODULES))
def test_removed_module_is_not_importable(filename):
    """Guards against the module reappearing anywhere else on sys.path."""
    module = filename[: -len(".py")]
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module)


def test_no_backend_module_imports_gridfs():
    """GridFS is not a storage path this application has.

    Source videos and frames live in S3; MongoDB holds accounts, ownership and
    job state only. An import of gridfs would mean a second, incompatible
    store had been reintroduced.
    """
    offenders = []
    for path in sorted(BACKEND_DIR.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if re.search(r"^\s*(import gridfs|from gridfs\b)", text, re.MULTILINE):
            offenders.append(path.name)
    assert not offenders, f"modules importing gridfs: {offenders}"


def test_no_backend_module_reads_the_duplicate_mongo_uri():
    """MONGODB_URL is the only connection-string variable.

    MONGO_URI was a second name for the same thing, read only by the removed
    GridFS pipeline. A new reader would reintroduce the collision where two
    variables configure one connection.
    """
    offenders = []
    for path in sorted(BACKEND_DIR.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if re.search(r"""getenv\(\s*["']MONGO_URI["']""", text):
            offenders.append(path.name)
    assert not offenders, f"modules reading MONGO_URI: {offenders}"


def test_no_module_hardcodes_a_mongo_connection_string():
    """A connection string belongs in the environment, not in source.

    verifier.py hardcoded one. The URL in db.py's os.getenv default is the
    local-development fallback and is allowed; an assignment of a literal
    mongodb:// URL to a module constant is not.
    """
    offenders = []
    for path in sorted(BACKEND_DIR.glob("*.py")):
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if re.match(r"""^[A-Z_]+\s*=\s*["']mongodb(\+srv)?://""", stripped):
                offenders.append(f"{path.name}:{number}")
    assert not offenders, f"hardcoded MongoDB connection strings: {offenders}"
