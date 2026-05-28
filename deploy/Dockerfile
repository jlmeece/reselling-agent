FROM python:3.11-slim

# System deps: cron for scheduling, curl for healthcheck pings
RUN apt-get update && apt-get install -y --no-install-recommends \
    cron \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (cached layer — only rebuilds when requirements.txt changes)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright's Chromium — required even for API-only modes because
# tools/costco_scraper.py is imported at module level. Chrome-dependent modes
# (active, daily, research, discovery) won't work on Linux until the Windows-specific
# Chrome launch code is replaced with Playwright's bundled Chromium.
RUN playwright install chromium --with-deps

# Copy project code
COPY . .

# Install crontab
COPY deploy/crontab /etc/cron.d/reselling-agent
RUN chmod 0644 /etc/cron.d/reselling-agent && crontab /etc/cron.d/reselling-agent

# Write cron output to stdout so docker logs captures it
RUN touch /var/log/cron.log

CMD ["sh", "-c", "cron && tail -f /var/log/cron.log"]
