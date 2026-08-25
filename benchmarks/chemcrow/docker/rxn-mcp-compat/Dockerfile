FROM rxn-mcp@sha256:71d5cd11ee5845f14f8eda3fadd849c11345a4e60e239a6dc35a58208b49eb30

# Upstream leaves both packages unconstrained. mcpo 0.0.20 imports the MCP 1.x
# streamable HTTP API and is incompatible with MCP 2.x.
RUN pip install --no-cache-dir --force-reinstall \
    "mcp==1.29.0" \
    "mcpo==0.0.20"
