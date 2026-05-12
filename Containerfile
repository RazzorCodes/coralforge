FROM python:3.12-slim

WORKDIR /app
ARG CACHE_BUST

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ src/

ENV PYTHONUNBUFFERED=1

EXPOSE 5000
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:5000", "src.app:create_app()"]
