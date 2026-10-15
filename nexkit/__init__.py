"""NexKit: turn GitHub issues into reviewed, tested pull requests with coding agents."""

__version__ = "3.2.0"

# NexKit runs on Python 3.12 or newer. CI tests 3.12 only, and the pipeline installs 3.12.
PYTHON = (3, 12)
# The pipeline installs exactly these. Each is tested; .github/workflows/update-agents.yml
# proposes the newest stable version of each runtime every week, and CI tests it before it
# is merged.
RUNNER = "ubuntu-24.04"
CLAUDE_CODE = "2.1.285"
CODEX = "0.162.1"


def python_supported(version_info):
    return tuple(version_info[:2]) >= PYTHON


def python_error(version_info):
    found = ".".join(str(part) for part in version_info[:3])
    wanted = ".".join(str(part) for part in PYTHON)
    return f"NexKit {__version__} requires Python {wanted} or newer; this is Python {found}."
