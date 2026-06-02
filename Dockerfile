FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY modules/ modules/
COPY cos/ cos/

RUN mkdir -p data/digests data/briefings

ENV PORT=8080
ENV USE_SECRET_MANAGER=true

EXPOSE 8080

CMD ["python", "mcp_server.py"]
