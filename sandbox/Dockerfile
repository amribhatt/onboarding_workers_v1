# The image every layout program and every ask-the-job query runs in. Engineer-owned. Built once and recorded by
# its image ID (a digest); the host runs it by digest, never by tag. No network is needed at run time.
#   docker build -t ow-sandbox:1 sandbox/
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
RUN pip install --no-cache-dir duckdb==1.5.6
COPY layout.py query.py /harness/
RUN chmod 0555 /harness /harness/layout.py /harness/query.py
USER 65534:65534
WORKDIR /tmp
