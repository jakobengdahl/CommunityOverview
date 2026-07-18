FROM python:3.12-slim

# Prevent Python from buffering stdout/stderr (important for Cloud Run logs)
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first so Docker can cache this layer.
# --require-hashes fails the build if requirements.txt ever loses its hash
# pins, so the image only ever ships hash-verified dependencies.
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

# Copy application source
COPY . .

# Cloud Run injects PORT at runtime; default to 8080
ENV PORT=8080

EXPOSE 8080

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
