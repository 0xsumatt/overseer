FROM python:3.12-slim

WORKDIR /app

# Install the package. Copy metadata + source first so the layer caches on deps.
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

# Scrape config (edit + rebuild, or mount over it via compose to change live).
COPY symbols.toml ./

# Run as a non-root user.
RUN useradd --create-home app
USER app

# The scheduler is the long-running service. Supervised via compose restart.
CMD ["overseer-scheduler"]