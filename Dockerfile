FROM python:3.7

WORKDIR /mnt

RUN apt-get install apt-transport-https ca-certificates

COPY requirements-dev.txt ./
COPY src/requirements.txt ./src/

RUN pip install --no-cache-dir -r requirements-dev.txt
