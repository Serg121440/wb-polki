FROM python:3.12-slim

# Московское время внутри контейнера
ENV TZ=Europe/Moscow
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Секреты передаются через переменные окружения (не COPY)
# GOOGLE_SA_JSON должен быть путём к файлу, который монтируется через volume,
# или содержимым, записанным в файл при старте контейнера (см. README).

CMD ["python", "scheduler.py"]
