FROM python:3.13-slim
WORKDIR /workspace
COPY app ./app
RUN mkdir -p /workspace/data /workspace/backups && useradd --system --uid 10001 workbench && chown -R workbench:workbench /workspace
USER workbench
ENV PYTHONUNBUFFERED=1 HOST=0.0.0.0 PORT=8765
EXPOSE 8765
VOLUME ["/workspace/data", "/workspace/backups"]
CMD ["python", "-m", "app.server"]
