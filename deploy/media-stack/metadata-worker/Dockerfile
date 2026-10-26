FROM python:3.13-slim
WORKDIR /worker
COPY worker/ /worker/
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
ENTRYPOINT ["python", "-u", "/worker/worker.py"]
