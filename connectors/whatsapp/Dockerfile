# Lobster WhatsApp Bridge — Docker image
# Node 18 + Chromium for Puppeteer (whatsapp-web.js requires a browser)

FROM node:18-slim

# Install Chromium and its dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    ca-certificates \
    fonts-liberation \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libc6 \
    libcairo2 \
    libcups2 \
    libdbus-1-3 \
    libexpat1 \
    libfontconfig1 \
    libgbm1 \
    libgcc1 \
    libglib2.0-0 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libstdc++6 \
    libx11-6 \
    libx11-xcb1 \
    libxcb1 \
    libxcomposite1 \
    libxcursor1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxi6 \
    libxrandr2 \
    libxrender1 \
    libxss1 \
    libxtst6 \
    lsb-release \
    wget \
    xdg-utils \
    && rm -rf /var/lib/apt/lists/*

# Tell Puppeteer to use the system Chromium (not download its own)
ENV PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true
ENV PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium

WORKDIR /app

# Copy package files first for layer caching
COPY package.json ./

# Install dependencies (skip optional puppeteer download)
RUN npm install --omit=optional 2>/dev/null || npm install

# Copy application code
COPY index.js ./
COPY test/ ./test/

# Create directories that the bridge expects
RUN mkdir -p /home/node/messages/wa-commands \
             /home/node/messages/wa-events \
             /home/node/lobster-workspace/logs

# Non-root user for security
USER node

# Environment variable defaults (override in docker-compose or -e flags)
ENV HOME=/home/node
ENV WA_COMMANDS_DIR=/home/node/messages/wa-commands
ENV WA_EVENTS_DIR=/home/node/messages/wa-events
ENV WA_HEARTBEAT_FILE=/home/node/lobster-workspace/logs/whatsapp-heartbeat
# WHATSAPP_SESSION_PATH is not set here — mount as volume or set via env
# WHATSAPP_LOBSTER_JID is not set here — set after first connection

CMD ["node", "index.js"]
