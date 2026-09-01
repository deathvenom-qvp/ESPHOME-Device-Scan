ARG BUILD_FROM
FROM ${BUILD_FROM}

ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /opt/edscan

# Build deps are needed only for the ruamel.yaml C extension; drop them after.
COPY requirements.txt ./
RUN apk add --no-cache --virtual .build-deps gcc musl-dev python3-dev \
    && pip install --no-cache-dir -r requirements.txt \
    && apk del .build-deps

COPY app/ ./app/
COPY scripts/ ./scripts/
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
