# Public upstream sources and model weights are acquired with fixtures.lock.json.
# A rebuild is a new RXN environment; record the resulting image identity.
FROM public.ecr.aws/docker/library/python:3.13-slim@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libexpat1 libxrender1 libxext6 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt constraints.txt ./
RUN pip install --no-cache-dir -r requirements.txt -c constraints.txt
COPY . .
RUN chmod +x start.sh && useradd -m -u 1000 rxn && chown -R rxn:rxn /app
USER rxn
ENTRYPOINT ["./start.sh"]
