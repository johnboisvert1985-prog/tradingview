FROM python:3.11-slim
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Créer l'utilisateur AVANT de chown
RUN useradd -m appuser

# Créer /mnt/data et donner les droits à appuser
RUN mkdir -p /mnt/data && chown -R appuser:appuser /mnt/data

COPY requirements.txt /app/
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY main.py /app/

USER appuser
ENV PORT=8000
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
