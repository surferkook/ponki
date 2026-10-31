# The house voice in a box: Kokoro-82M (Apache 2.0) with two voices,
# Heart and Puck. Used by `docker compose --profile voice up`.
#
# The model and the two voices are fetched at build time from the
# canonical repo (hexgrad/Kokoro-82M) at a pinned revision, so the
# running service never downloads anything. The Piper voices are not
# included: their licences don't allow passing them on.

FROM python:3.10.22-slim-bookworm
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/opt/hf HOUSE_VOICES=af_heart,am_puck
WORKDIR /voice
COPY requirements.txt ./
RUN pip install -r requirements.txt \
 && pip install https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl

# hexgrad/Kokoro-82M at a fixed revision; Kokoro asks for "main", so main
# is pointed at that revision in the cache.
ARG KOKORO_REVISION=f3ff3571791e39611d31c381e3a41a3af07b4987
RUN python -c "import sys; from huggingface_hub import hf_hub_download as get; \
[get('hexgrad/Kokoro-82M', f, revision=sys.argv[1]) for f in \
('config.json', 'kokoro-v1_0.pth', 'voices/af_heart.pt', 'voices/am_puck.pt')]" "$KOKORO_REVISION" \
 && mkdir -p "$HF_HOME/hub/models--hexgrad--Kokoro-82M/refs" \
 && printf %s "$KOKORO_REVISION" > "$HF_HOME/hub/models--hexgrad--Kokoro-82M/refs/main" \
 && chmod -R a+rX "$HF_HOME"
ENV HF_HUB_OFFLINE=1

COPY voice_server.py ./
RUN useradd --create-home --uid 1000 ponki
USER ponki
CMD ["uvicorn", "voice_server:app", "--host", "0.0.0.0", "--port", "8202"]
