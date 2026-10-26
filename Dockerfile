FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN groupadd --system questify \
    && useradd --system --gid questify questify \
    && chown -R root:questify /app \
    && chmod -R g+rX /app

USER questify

EXPOSE 8000

CMD ["sh", "-c", "python manage.py check --database default && case \"${DEBUG:-false}\" in 1|[Tt][Rr][Uu][Ee]|[Yy][Ee][Ss]|[Oo][Nn]) ;; *) python manage.py check --deploy --database default --fail-level ERROR ;; esac && python manage.py collectstatic --noinput && exec gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers ${GUNICORN_WORKERS:-3} --access-logfile - --error-logfile -"]
