FROM python:3.12-slim
WORKDIR /app
COPY hackergit.py server.py ./
COPY static ./static
ENV PORT=8080 HACKERGIT_DB=/data/hackergit.db PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python", "server.py"]
