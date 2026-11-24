# Runner image for the schemaops pipeline: git, pt-online-schema-change and the Python
# dependencies. schemaops itself is installed from the repository by each job, so the
# image never goes stale when the tool changes.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git percona-toolkit \
    && rm -rf /var/lib/apt/lists/*

# Keep in step with pyproject.toml.
RUN pip install --no-cache-dir "PyYAML>=6.0" "PyMySQL>=1.1" "pytest>=8.0"
