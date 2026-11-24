"""NexKit's tests.

They run git as CI does, whatever the machine has: with no global or system configuration, so
that a machine that signs commits or sets other defaults gets the commits, and so the SHAs,
that the goldens hold. The tests set the identities they commit as themselves."""

import os

os.environ["GIT_CONFIG_GLOBAL"] = os.devnull
os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
