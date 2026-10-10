# The isolated PDF parser (DI-1): one container per document, run by the worker
# with no network, a read-only root, no capabilities and resource limits
# (apps/api/src/api/sandbox.py). The base image is pinned by its multi-arch
# index digest; dependencies are hash-locked wheels (requirements.txt, exported
# from uv.lock); pip is removed afterwards.
FROM python:3.14-slim@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY requirements.txt /tmp/requirements.txt
RUN python -m pip install --require-hashes --no-deps --only-binary=:all: -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt \
    && python -m pip uninstall --yes pip

# Plain source (no build backend in the image), compiled ahead of time because
# the root filesystem is read-only at run time.
COPY src/docparser /usr/local/lib/python3.14/site-packages/docparser
RUN python -m compileall -q /usr/local/lib/python3.14/site-packages/docparser

LABEL org.production-ai-flagship.role=parser
USER 65534:65534
# The supervisor runs the parser (python -I -m docparser) as its only child and
# reports a kernel OOM kill from the cgroup's own counter (docparser/supervise.py).
ENTRYPOINT ["python", "-I", "-m", "docparser.supervise"]
