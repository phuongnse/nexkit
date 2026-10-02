abi <abi/4.0>,
include <tunables/global>

# Explicit namespace permission for these runner containers on Ubuntu 24.04.
# Other host applications retain the system's user-namespace restrictions.
# Docker capabilities, the seccomp profile and Codex's nested sandbox remain
# separate enforced boundaries.
profile nexkit-runner flags=(unconfined) {
  userns,
}
