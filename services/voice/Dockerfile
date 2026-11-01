# The house voice in a box: Meta's MMS-TTS (facebook/mms-tts-eng,
# CC-BY-NC 4.0), run on the CPU. Used by `docker compose --profile voice up`.
#
# The model is fetched at build time from the canonical repo at a pinned
# revision, so the running service never downloads anything.

FROM python:3.10.22-slim-bookworm
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/opt/hf
WORKDIR /voice
COPY requirements.txt ./
RUN pip install -r requirements.txt

# facebook/mms-tts-eng at a fixed revision (voice_server.py asks for the same one)
ARG MMS_REVISION=c71de0fe7204c83f1c10820a7d696d0b450048ba
ENV MMS_REVISION=$MMS_REVISION
RUN python -c "import sys; from huggingface_hub import hf_hub_download as get; \
[get('facebook/mms-tts-eng', f, revision=sys.argv[1]) for f in \
('config.json', 'model.safetensors', 'vocab.json', 'tokenizer_config.json', 'special_tokens_map.json')]" "$MMS_REVISION" \
 && chmod -R a+rX "$HF_HOME"
ENV HF_HUB_OFFLINE=1

COPY voice_server.py ./
RUN useradd --create-home --uid 1000 ponki
USER ponki
CMD ["uvicorn", "voice_server:app", "--host", "0.0.0.0", "--port", "8202"]
