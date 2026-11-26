# The default image: what a package runs in when it declares no image of its own.
#
# The design (designs/how-optpilot-runs-code.md §4) fixes what any image must
# provide, and this one provides exactly that and nothing more:
#
#   - a Python interpreter on the path as python3, at OptPilot's minimum version
#   - a widely-used Linux base with the common C library, because OptPilot
#     mounts its launcher in as source and its editing tools as prebuilt
#     binaries, and those need glibc
#   - a writable temporary directory
#   - a default user that is not root
#
# Deliberately absent: OptPilot itself (mounted in at run time, never installed,
# so this image never tracks OptPilot's version), and any start command (OptPilot
# supplies the command; anything declared here would be ignored).
#
# Published by the automated build in .github/workflows/default-image.yml --
# never from an individual's machine -- public from its first publication, and
# never deleted, because every run that used it records its fingerprint and
# deleting it would make them all unrepeatable at once.

FROM python:3.12-slim-bookworm

# A fixed, unprivileged identity. Runs never need root, and the design says a
# default user that is not root is part of what an image must provide.
RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin optpilot
USER optpilot

# No ENTRYPOINT and no CMD: OptPilot supplies the command itself and ignores
# any the image declares. Leaving them out makes that fact visible here too.
