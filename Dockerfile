FROM python:3.11-slim

# Crée un venv system-wide propre
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app
# Crée le dossier attendu par main.py pour éviter FileNotFoundError
RUN mkdir -p /mnt/data
RUN mkdir -p /mnt/data && chown -R appuser:appuser /mnt/data
COPY requirements.txt /app/
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY main.py /app/

# User non-root
RUN useradd -m appuser
USER appuser

# Render/Heroku-style PORT
ENV PORT=8000
EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]

