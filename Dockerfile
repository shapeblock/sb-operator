FROM python:3.8
WORKDIR /src
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
CMD kopf run --liveness=http://0.0.0.0:8080/healthz sb.py
EXPOSE 8080
