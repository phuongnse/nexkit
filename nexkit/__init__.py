"""NexKit: turn GitHub issues into reviewed, tested pull requests with Claude Code."""

__version__ = "1.3.1"

# NexKit runs on Python 3.12 or newer. CI tests 3.12 only, and the pipeline installs 3.12.
PYTHON = (3, 12)
# The pipeline installs exactly these. Each is tested; .github/workflows/update-claude-code.yml
# proposes the newest stable Claude Code every week, and CI tests it before it is merged.
RUNNER = "ubuntu-24.04"
CLAUDE_CODE = "2.1.285"


def python_supported(version_info):
    return tuple(version_info[:2]) >= PYTHON


def python_error(version_info):
    found = ".".join(str(part) for part in version_info[:3])
    wanted = ".".join(str(part) for part in PYTHON)
    return f"NexKit {__version__} requires Python {wanted} or newer; this is Python {found}."
