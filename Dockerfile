# Same base image the commits were labeled in (its Python decides which tests run).
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ENV PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

# The labeling environment's packages, so sqlglot's tests behave as they did there.
COPY requirements.lock /app/requirements.lock
RUN pip install -r /app/requirements.lock

# sqlglot at the pinned commit, straight from its repository.
ARG SQLGLOT_SHA=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
RUN python -c "import urllib.request, tarfile, io; \
data = urllib.request.urlopen('https://codeload.github.com/tobymao/sqlglot/tar.gz/${SQLGLOT_SHA}').read(); \
tarfile.open(fileobj=io.BytesIO(data)).extractall('/app', filter='data')" \
 && mv /app/sqlglot-${SQLGLOT_SHA} /app/sqlglot \
 && python -m compileall -q -j 0 /app/sqlglot/sqlglot /app/sqlglot/tests \
 && chmod -R a+rX /app

COPY selector.py analyze.py runner.py split.py map.json.gz cases.json.gz /app/
COPY ops /app/ops
RUN python -m compileall -q /app/*.py \
 && chmod -R a+rX /app

WORKDIR /app
CMD ["python", "/app/selector.py"]
