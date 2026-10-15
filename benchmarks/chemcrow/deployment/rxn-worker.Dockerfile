# Public upstream sources and model weights are acquired with fixtures.lock.json.
# A rebuild is a new RXN environment; record the resulting image identity.
FROM public.ecr.aws/docker/library/python:3.13-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libexpat1 libxrender1 libxext6 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN chmod +x start.sh && useradd -m -u 1000 rxn && chown -R rxn:rxn /app
USER rxn
ENTRYPOINT ["./start.sh"]
