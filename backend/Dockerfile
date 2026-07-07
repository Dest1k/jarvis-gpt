FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV JARVIS_HOME=/runtime

WORKDIR /app

COPY requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

COPY src /app/backend/src

ENV PYTHONPATH=/app/backend/src
EXPOSE 8000

CMD ["uvicorn", "jarvis_gpt.main:app", "--host", "0.0.0.0", "--port", "8000"]
