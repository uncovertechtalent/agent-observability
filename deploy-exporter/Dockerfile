FROM python:3.12-slim
WORKDIR /app
COPY exporter.py .
# /data holds the seen-runs state; a new named volume takes this ownership
RUN mkdir /data && chown nobody /data
USER nobody
EXPOSE 9201
CMD ["python", "exporter.py"]
