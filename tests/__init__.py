"""NexKit's tests.

They run git as CI does, whatever the machine has: with no global or system configuration, and
none passed through the environment, so that a machine that signs commits or sets other
defaults gets the commits, and so the SHAs, that the goldens hold. The tests set the identities
they commit as themselves."""

import os

os.environ["GIT_CONFIG_GLOBAL"] = os.devnull
os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
for _name in list(os.environ):
    if _name == "GIT_CONFIG_PARAMETERS" or _name.startswith("GIT_CONFIG_COUNT"):
        del os.environ[_name]
    elif _name.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
        del os.environ[_name]
