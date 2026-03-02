FROM python:3.13-slim

WORKDIR /app

# System dependencies for Discord voice:
#   libopus0  — Opus codec (required for audio encode/decode in py-cord)
#   libsodium23 — encryption (PyNaCl bundles its own but this avoids edge cases)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libopus0 \
    libsodium23 \
    && rm -rf /var/lib/apt/lists/*

# Install deps first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY . .

# Directories that need to exist at runtime
RUN mkdir -p data/memory logs

# Railway injects PORT; default to 8080
ENV PORT=8080

CMD ["python", "main.py"]
