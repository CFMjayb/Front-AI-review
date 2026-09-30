FROM python:3.12-slim
# Apply Debian security updates published since the base image was built.
# Added 2026-09-30 for CVE-2026-84782 (openssl, HIGH): the scanner flagged it
# as fixable once Debian shipped the patch, but the slim base had not been
# refreshed yet. Upgrading here clears that class on every rebuild.
RUN apt-get update && apt-get upgrade -y --no-install-recommends && rm -rf /var/lib/apt/lists/*
# Closes CVE-2026-24049 (wheel) / CVE-2026-23949 (jaraco.context). The scanner
# flags the copies vendored INSIDE the base image's setuptools
# (setuptools/_vendor/wheel-0.45.1, jaraco.context-5.3.0), so upgrading the
# top-level packages alone does not clear them (tried 2026-09-22). setuptools
# 81.x is the first release vendoring fixed copies (wheel 0.46.3,
# jaraco_context 6.1.0); <82 because 82 removed pkg_resources (2026-09-25).
RUN pip install --no-cache-dir --upgrade "setuptools>=81,<82" "wheel>=0.46.2" "jaraco.context>=6.1.0"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY modules/ modules/
COPY cos/ cos/

RUN mkdir -p data/digests data/briefings

ENV PORT=8080
ENV USE_SECRET_MANAGER=true

EXPOSE 8080

CMD ["python", "mcp_server.py"]
