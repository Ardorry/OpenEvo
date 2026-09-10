FROM rxn-mcp:rerun-20260830-historical-base

# Fresh-replication compatibility layer. The source image identity is recorded
# separately; these versions match the sealed full-v5 runtime manifest.
RUN pip install --no-cache-dir --force-reinstall \
    "mcp==1.29.0" \
    "mcpo==0.0.20" \
    "click==8.4.2" \
    "cryptography==50.0.0" \
    "pydantic==2.13.4" \
    "pydantic-core==2.46.4" \
    "typer==0.27.1" \
    "wcwidth==0.8.2"
