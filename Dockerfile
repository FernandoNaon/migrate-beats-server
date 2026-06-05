# Dockerfile for Migrate Beats Server
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies for psycopg2
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Expose port (Railway sets PORT dynamically)
EXPOSE 5000

# Run with gunicorn - Railway provides $PORT env var.
# --timeout 300 because archaeologist snapshot builds make 30-80 Spotify API calls
# back-to-back and can exceed gunicorn's default 30s worker timeout.
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT:-5000} --workers 2 --timeout 300 app:app"]
