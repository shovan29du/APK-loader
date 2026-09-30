FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends adb && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app app
COPY static static
ENV ADB_SERIAL=android:5555
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
