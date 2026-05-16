FROM python:3.13-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends openssh-client sshpass ca-certificates fonts-dejavu-core fonts-noto-cjk curl gnupg librsvg2-bin \
 && curl -fsSL https://deb.nodesource.com/setup_24.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*
COPY telegram-bot/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
RUN npm install -g playwright@1.59.1 \
 && PLAYWRIGHT_BROWSERS_PATH=/ms-playwright npx playwright install --with-deps chromium
ENV NODE_PATH=/usr/lib/node_modules PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
COPY auth.py guko.py jiaoops.py /app/
RUN mkdir -p /data/keys /data/media /data/tmp
COPY telegram-bot/bot.py telegram-bot/render_checkplace.py /app/
COPY telegram-bot/tools/ /app/tools/
ENV BGP_FETCH=/app/tools/bgp_fetch.py IPPURE_DOWNLOAD=/app/tools/download_ippure.js
CMD ["python", "/app/bot.py"]
