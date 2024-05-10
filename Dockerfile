ARG PYVERSION=3.10

FROM python:${PYVERSION}-slim as python-reqs

COPY ./requirements.txt requirements.txt

RUN apt update && apt install git libxml2-dev libxslt-dev build-essential -y 
RUN pip3 install --no-cache-dir -r requirements.txt

FROM python:${PYVERSION}-slim

COPY --from=python-reqs /usr/local/lib/python3.9/site-packages /usr/local/lib/python3.9/site-packages

WORKDIR /app
COPY src .

CMD ["python3", "/app/main.py"]