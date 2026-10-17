FROM python:3.12-slim
WORKDIR /app
# Every dependency is pinned with its hash (requirements.in, compiled with pip-compile --generate-hashes).
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
COPY core.py diagnose.py bot.py ./
# Runs as the host user that owns the token file (mode 600), so the read-only mount is readable.
# /data holds the incident database; a new named volume takes this ownership.
ARG UID=1000
ARG GID=1000
RUN mkdir /data && chown ${UID}:${GID} /data
USER ${UID}:${GID}
EXPOSE 9096
CMD ["python", "bot.py"]
