FROM python:3.12-slim
WORKDIR /app
COPY crm.py ./
COPY static ./static
# No third-party dependencies: Python standard library only.
ENV HOST=0.0.0.0 PORT=8080 CRM_DB=/data/crm.db
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"
CMD ["python3", "crm.py"]
