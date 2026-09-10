FROM public.ecr.aws/docker/library/python@sha256:ffb752e139c0a19692a43af8d8523b274222dd68eebad5d583b45c2201c6e30a

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir \
    "celery==5.6.3" \
    "mcp==2.1.0" \
    "mcpo==0.0.20" \
    "redis==8.1.0" \
    "mcp-types==2.1.0" \
    "click==8.4.2" \
    "cryptography==50.0.0" \
    "pydantic==2.13.4" \
    "pydantic-core==2.46.4" \
    "typer==0.27.1" \
    "wcwidth==0.8.2"

COPY server.py start.sh .
RUN chmod +x start.sh

CMD ["./start.sh"]
