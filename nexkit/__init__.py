"""NexKit: turn GitHub issues into reviewed, tested pull requests with coding agents."""

__version__ = "6.13.0"

# NexKit runs on Python 3.12 or newer. CI tests 3.12 only, and the pipeline installs 3.12.
PYTHON = (3, 12)
# The base of the engine's image (Dockerfile): that Python, by the digest of its index.
# .github/workflows/update-image.yml proposes the tag's current digest every week.
PYTHON_IMAGE = (
    "python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b"
)
# The pipeline installs exactly these. Each is tested; .github/workflows/update-agents.yml
# proposes the newest stable version of each runtime every week, and CI tests it before it is
# merged.
RUNNER = "ubuntu-24.04"
CLAUDE_CODE = "2.1.323"


def python_supported(version_info):
    return tuple(version_info[:2]) >= PYTHON


def python_error(version_info):
    found = ".".join(str(part) for part in version_info[:3])
    wanted = ".".join(str(part) for part in PYTHON)
    return f"NexKit {__version__} requires Python {wanted} or newer; this is Python {found}."
