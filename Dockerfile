FROM python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10010 --create-home lab10
COPY app.py pg_store.py migrate_sqlite.py workbench.html retry_telegram.py employees.csv employees.geojson offices.geojson zones.geojson common-zone.geojson teacher-matrix.csv ./
COPY assets ./assets
USER 10010:10010
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
