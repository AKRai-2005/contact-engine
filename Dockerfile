# Contact Engine - runnable image.
# Build:  docker build -t contact-engine .
# Harvest:  docker run --rm -v "$PWD/data:/data" contact-engine harvest example.com --db /data/contacts.db
# Enrich:   docker run --rm -v "$PWD/data:/data" contact-engine enrich "Jane Doe" example.com --db /data/contacts.db
# API:      docker run --rm -p 8000:8000 -v "$PWD/data:/data" --entrypoint uvicorn contact-engine api:app --host 0.0.0.0 --port 8000
#
# SMTP verification needs OUTBOUND PORT 25, which most hosts block. Run verification only
# where port 25 is open (a suitable VPS); elsewhere every probe returns
# "port_25_blocked_or_timeout" and you fall back to pattern confidence.
FROM python:3.12-slim

WORKDIR /app

# deps first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# the store lives on a mounted volume so data survives container restarts
VOLUME ["/data"]

ENTRYPOINT ["python", "engine.py"]
CMD ["stats", "--db", "/data/contacts.db"]
