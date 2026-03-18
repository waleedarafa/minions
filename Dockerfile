FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    git \
    curl \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install uv for fast package installation
RUN pip install uv

# Copy all source files first (needed for hatchling to find README.md)
COPY . .

# Install Python dependencies
RUN uv pip install --system -e ".[dev]"

# Default command
CMD ["python", "-m", "src.cli.main", "--help"]
