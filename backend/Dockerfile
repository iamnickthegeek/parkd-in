# PH6-048 — Pin base image for reproducibility and add GDAL/osmnx system deps.
FROM python:3.11-slim-bookworm

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# Set work directory
WORKDIR /app

# PH6-048 — Install GDAL, osmnx, and spatial index system libraries.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    gcc \
    libgdal-dev \
    gdal-bin \
    libspatialindex-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies (Expects requirements.txt at the build context root)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project
COPY . .

# Expose port
EXPOSE 8000

# Command to run (will be overridden by docker-compose for dev)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
