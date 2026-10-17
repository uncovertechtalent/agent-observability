FROM python:3.12-slim
WORKDIR /app
COPY receiver.py .
# Runs as the host user that owns the token file (mode 600), so the read-only mount is readable.
# /data holds the fingerprint-to-issue map; a new named volume takes this ownership.
ARG UID=1000
ARG GID=1000
RUN mkdir /data && chown ${UID}:${GID} /data
USER ${UID}:${GID}
EXPOSE 9095
CMD ["python", "receiver.py"]
