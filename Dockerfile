FROM python:3.12-slim
WORKDIR /app
# rsvg-convert renders the per-repo share cards (/og/<owner>/<repo>.png); Inter is the font they're drawn in
RUN apt-get update && apt-get install -y --no-install-recommends librsvg2-bin fonts-inter fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
COPY hackergit.py server.py ./
COPY static ./static
ENV PORT=8080 HACKERGIT_DB=/data/hackergit.db PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python", "server.py"]
