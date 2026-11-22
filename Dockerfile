# One image, two jobs:
#   docker run -d --restart=always -v $PWD/data:/app/data --env-file .env nycsavant collect
#   docker run --rm -v $PWD/data:/app/data -v $PWD/site:/app/site --env-file .env nycsavant nightly
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY savant savant
COPY tools tools
ENV TZ=America/New_York
ENTRYPOINT ["python", "-m", "savant"]
CMD ["collect"]
