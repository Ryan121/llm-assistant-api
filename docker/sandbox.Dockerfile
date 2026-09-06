# The container the agent's `run` tool executes in.
#
# Nothing about this image is precious. It holds no state worth keeping - the
# workspace is bind-mounted from the host, so every file that matters lives
# outside it - and the container built from it is meant to be thrown away and
# rebuilt whenever it gets large or confusing:
#
#     make sandbox-wipe
#
# Kept deliberately close to the deployment: the same Python as the gateway, so
# a test run in here means what it says on the host.

FROM python:3.13-slim

# git, because the agent reads its own diff; the compilers, because a wheel
# without a manylinux build will otherwise fail to install and the agent will
# spend three turns working out why.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        git \
        make \
    && rm -rf /var/lib/apt/lists/*

# The container runs as the host user (`--user $(id -u):$(id -g)`), so files
# the agent creates in the bind-mounted workspace stay yours rather than
# arriving owned by root. That user owns nothing in the image, so anything
# needing a writable HOME gets /tmp, and tools are installed somewhere
# world-readable rather than into a home directory that will not exist.
ENV PIP_ROOT_USER_ACTION=ignore \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp

RUN pip install --no-cache-dir uv ruff mypy pytest

# git refuses to operate on a repository owned by a different uid than the
# caller. The bind mount is owned by the host user, which inside the container
# is a uid with no passwd entry, so this is the one thing that needs saying.
RUN git config --system --add safe.directory /workspace

WORKDIR /workspace

# The agent's sandbox masks these paths with anonymous volumes so that build
# artefacts compiled for the host's platform are never shared with - or, far
# worse, overwritten by - the container's. Docker initialises such a volume
# from whatever the image has at that path, ownership and mode included, and
# the container runs as the host's uid, which owns nothing in this image. So
# they are created here, world-writable, or the container could not write to
# its own scratch directories. Keep in step with MASKED in sandbox.py.
RUN mkdir -p \
        /workspace/.venv \
        /workspace/venv \
        /workspace/node_modules \
        /workspace/.mypy_cache \
        /workspace/.pytest_cache \
        /workspace/.ruff_cache \
        /workspace/.tox \
    && chmod 1777 /workspace/*[a-z] /workspace/.[a-z]*

# Overridden by `docker run ... sleep infinity`; the container is a place to
# exec into, not a process that does anything on its own.
CMD ["sleep", "infinity"]
