FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY exporter.py .
USER nobody
EXPOSE 11435
CMD ["python", "exporter.py"]
