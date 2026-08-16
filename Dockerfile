# ChattyDesk backend as a Docker image.
#
# Built by docker compose from the repo root, or directly:
#   docker build -t chattydesk-backend .
#   docker run --rm -p 9001:9001 --env-file .env \
#     -e PORT=9001 \
#     -e DJANGO_ALLOWED_HOSTS=backend,backend:9001,localhost,127.0.0.1 \
#     -v "$PWD/db.sqlite3:/app/db.sqlite3" \
#     chattydesk-backend
#
# The .env file is deliberately NOT copied into the image — pass it at run
# time with --env-file (or the compose env_file) so secrets stay off the
# build context.

FROM python:3.12-slim

# Run as uid 1000 so a bind-mounted db.sqlite3 stays writable on the host.
RUN useradd --create-home --uid 1000 app

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/staticfiles && chown -R app:app /app

USER app

# gunicorn listens on this port inside the container (see deploy.sh).
ENV PORT=9001

EXPOSE 9001

# deploy.sh collects static files, runs migrations, then boots gunicorn.
# Dependencies were installed above, so skip the pip step.
CMD ["./deploy.sh", "--skip-install"]
