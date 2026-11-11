FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py ./
COPY routes/ routes/
COPY static/ static/
COPY templates/ templates/
ARG BUILD_DATE=""
RUN date +%s%N > /app/.build_time
# Kör som en vanlig användare, aldrig som root. Datamappen på värden ska ägas av
# uid/gid 10001 (sudo chown -R 10001:10001 <datamapp>).
RUN groupadd --system --gid 10001 docharbor \
 && useradd --system --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin docharbor
USER 10001:10001
EXPOSE 8080
# En worker (backupjobben håller sitt tillstånd i minnet), flera trådar.
CMD ["gunicorn", "--workers", "1", "--threads", "8", "--timeout", "600", \
     "--bind", "0.0.0.0:8080", "--no-control-socket", "--access-logfile", "-", \
     "--forwarded-allow-ips", "*", "app:app"]
