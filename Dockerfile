FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN groupadd --system questify \
    && useradd --system --gid questify questify \
    && chown -R questify:questify /app

USER questify

EXPOSE 8000

CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
