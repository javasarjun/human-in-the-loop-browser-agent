# Build image for the Streamlit + Playwright app on AWS App Runner.
#
# We start from Microsoft's official Playwright-Python base image. It already
# has Chromium + the long list of system libs Playwright needs, which saves
# ~5 minutes of build time and avoids version drift between local Playwright
# and the container's Chromium.
#
# If you bump the Playwright version in requirements.txt, also bump the tag here.
FROM mcr.microsoft.com/playwright/python:v1.60.0-jammy

WORKDIR /app

# Install Python deps first so this layer caches when only app code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy only the modules Streamlit actually needs at runtime.
# (backend/, venv/, uploads/, .env, etc. are excluded via .dockerignore.)
COPY app.py \
     browser_agent.py \
     dynamodb_service.py \
     llm_service.py \
     processing_service.py \
     submitter_service.py \
     upload_service.py \
     ./

# Streamlit's default port.
EXPOSE 8501

# Streamlit-specific flags:
#   --server.address=0.0.0.0  bind so App Runner's load balancer can reach us
#   --server.headless=true    don't try to open a browser (no GUI in a container)
#   --server.enableCORS=false simplifies running behind App Runner's ALB
#   gatherUsageStats=false    skip anonymous telemetry
CMD ["streamlit", "run", "app.py", \
     "--server.address=0.0.0.0", \
     "--server.port=8501", \
     "--server.headless=true", \
     "--server.enableCORS=false", \
     "--browser.gatherUsageStats=false"]
