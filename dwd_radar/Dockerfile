FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

COPY app /app/app
COPY www /app/www
ARG BUILD_VERSION=dev
ENV ADDON_VERSION=${BUILD_VERSION}

WORKDIR /app/app
CMD ["python", "-u", "main.py"]

LABEL io.hass.type="addon" io.hass.version="${BUILD_VERSION}"
