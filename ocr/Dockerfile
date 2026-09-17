# OCR-сервис «Домашки»: посимвольное распознавание рукописи без языковой модели.
FROM python:3.12-slim
WORKDIR /app
COPY ocr/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY ocr ./ocr
RUN useradd --uid 1000 --create-home app && chown -R app:app /app
USER app
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health')"
CMD ["python", "-m", "ocr.server"]
