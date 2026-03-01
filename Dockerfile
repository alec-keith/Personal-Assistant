FROM python:3.13-slim

WORKDIR /app

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
