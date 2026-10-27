# Mock hubs as one image. The contracts are copied from .contracts/ (make contracts) so the image says
# exactly which hub-contracts commit it was built from.
FROM python:3.13-slim
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .
COPY .contracts /contracts
COPY contracts.lock /contracts.lock
ENV EV_PNC_CONTRACTS_DIR=/contracts
RUN useradd --system --uid 10001 sim
USER sim
EXPOSE 8080
CMD ["python", "-m", "ev_pnc_simulator.hubject", "--host", "0.0.0.0", "--port", "8080"]
