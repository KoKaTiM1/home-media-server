FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core ca-certificates && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir Pillow==12.2.0
WORKDIR /app
COPY app.py /app/app.py
COPY features.py /app/features.py
COPY reliability.py /app/reliability.py
COPY metadata_jobs.py /app/metadata_jobs.py
ENTRYPOINT ["python", "-u", "/app/app.py"]

COPY verification_media.py /app/verification_media.py
COPY verification_jobs.py /app/verification_jobs.py
COPY verification_remote.py /app/verification_remote.py
