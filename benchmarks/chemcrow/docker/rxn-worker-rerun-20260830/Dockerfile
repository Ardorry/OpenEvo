FROM public.ecr.aws/docker/library/python@sha256:ffb752e139c0a19692a43af8d8523b274222dd68eebad5d583b45c2201c6e30a

WORKDIR /app

RUN apt-get update \
    && apt-get install -y \
        libexpat1 \
        libxrender1 \
        libxext6 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# The upstream requirements were unconstrained. Pin the versions recorded by
# the sealed full-v5 RXN runtime manifest/build log for this fresh replication.
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir \
        "celery[redis]==5.3.6" \
        "lightning==2.6.5" \
        "Levenshtein==0.27.4" \
        "rdkit==2026.3.5" \
        "redis==5.3.1" \
        "torch==2.13.0" \
        "transformers==4.57.6" \
        "rxn-chem-utils==1.6.0" \
        "rxn-reaction-preprocessing==2.6.0" \
        "numpy==2.5.2" \
        "jsonargparse[signatures]==4.51.0" \
        "pydantic==2.13.4" \
        "pydantic-core==2.46.4" \
        "click==8.4.2" \
        "wcwidth==0.8.2"

COPY . .
RUN chmod +x start.sh
RUN useradd -m -u 1000 rxn && chown -R rxn:rxn /app

USER rxn
ENTRYPOINT ["./start.sh"]
