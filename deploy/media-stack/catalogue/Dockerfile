FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core ca-certificates && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir Pillow==12.2.0
WORKDIR /app
COPY app.py /app/app.py
COPY features.py /app/features.py
ENTRYPOINT ["python", "-u", "/app/app.py"]
