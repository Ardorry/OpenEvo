FROM public.ecr.aws/docker/library/python:3.13-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt 'mcp==1.29.0' 'mcpo==0.0.20'
COPY server.py start.sh ./
RUN chmod +x start.sh
CMD ["./start.sh"]
