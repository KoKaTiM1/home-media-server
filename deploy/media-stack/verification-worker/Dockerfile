FROM home-media-catalogue:1
USER root
RUN rm -rf /app
WORKDIR /worker
COPY verification-worker/verification_worker.py verification-worker/local_verifier.py /worker/
COPY catalogue/*.py /worker/
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
ENTRYPOINT ["python", "-u", "/worker/verification_worker.py"]
