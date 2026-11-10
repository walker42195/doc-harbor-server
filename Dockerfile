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
EXPOSE 8080
CMD ["python", "app.py"]
