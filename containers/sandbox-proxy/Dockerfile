FROM python:3.12-slim
WORKDIR /app
COPY proxy.py .
ENV UPSTREAM_URL=http://sandbox:9999
ENV LOG_PATH=/logs/sandbox-proxy.ndjson
ENV LISTEN_PORT=9998
EXPOSE 9998
CMD ["python", "-u", "proxy.py"]
