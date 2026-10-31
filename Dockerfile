# Ponki in a box: one image for the hub, the finding service (SAM 3.1) and
# a workshop. compose.yaml runs it three ways; see the README.
#
# The SAM mask decoder is a small Node worker (@meta-sam/parser needs
# Node >= 20.17): built in the official Node image, then copied over.

FROM node:20.20.2-bookworm-slim AS js
WORKDIR /js
COPY services/sam/js/package.json services/sam/js/package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund

FROM python:3.10.22-slim-bookworm
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY --from=js /usr/local/bin/node /usr/local/bin/node
RUN apt-get update && apt-get install -y --no-install-recommends libstdc++6 \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /ponki
COPY requirements.txt ./
COPY services/sam/requirements.txt services/sam/requirements.txt
RUN pip install -r requirements.txt -r services/sam/requirements.txt
COPY . .
COPY --from=js /js/node_modules services/sam/js/node_modules
RUN useradd --create-home --uid 1000 ponki \
 && mkdir -p /data/hub /data/workshop \
 && chown -R ponki /data \
 && chmod +x deploy/docker/entrypoint.sh
USER ponki
ENTRYPOINT ["/ponki/deploy/docker/entrypoint.sh"]
CMD ["uvicorn", "hub.main:app", "--host", "0.0.0.0", "--port", "8200"]
