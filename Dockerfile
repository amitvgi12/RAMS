# RAMS — single stdlib process, zero dependencies. Runs on any container host
# (Fly.io, Google Cloud Run, Koyeb, Hugging Face Spaces, Render-as-Docker, ...).
FROM python:3.12-slim

WORKDIR /app
COPY . /app

# No pip install: RAMS is pure standard library.
ENV PORT=8080
EXPOSE 8080

# Honour the platform's $PORT (Cloud Run/Render=injected, HF Spaces=7860, else 8080).
CMD ["sh", "-c", "python -m rams.server --host 0.0.0.0 --port ${PORT:-8080}"]
