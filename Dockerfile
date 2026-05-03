FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY modules/ modules/

RUN mkdir -p data/digests

ENV PORT=8080
ENV USE_SECRET_MANAGER=true

EXPOSE 8080

CMD ["python", "mcp_server.py"]
