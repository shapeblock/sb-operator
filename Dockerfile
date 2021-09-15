FROM python:3.8
COPY . /src/
WORKDIR /src
RUN pip install -r requirements.txt
CMD kopf run sb.py