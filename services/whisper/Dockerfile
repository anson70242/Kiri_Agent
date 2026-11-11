FROM nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04

# Avoid interactive prompts during package install
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && \
    apt-get install -y --no-install-recommends python3 python3-pip && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

# Download the model at build time so startup is fast
# This layer is cached — only re-runs if requirements.txt changes
RUN python3 -c "from faster_whisper import WhisperModel; WhisperModel('large-v3-turbo', device='cpu')"

COPY server.py .

EXPOSE 8000

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
