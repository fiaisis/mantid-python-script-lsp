# syntax=docker/dockerfile:1

# The service is only useful if it agrees with what actually runs the scripts, so the base is a
# Mantid runner image pinned to a runner tag. Moving it is a deliberate act - it changes which names a
# script may use and what warm-up costs - never a rebuild side effect.
ARG RUNNER_IMAGE=ghcr.io/fiaisis/mantid
ARG RUNNER_TAG=7.0.0
FROM ${RUNNER_IMAGE}:${RUNNER_TAG}

ARG RUNNER_IMAGE
ARG RUNNER_TAG

LABEL org.opencontainers.image.title="mantid-python-script-lsp" \
      org.opencontainers.image.description="Mantid-aware Python analysis for the FIA script editors" \
      org.opencontainers.image.base.name="${RUNNER_IMAGE}:${RUNNER_TAG}"

WORKDIR /mantid_python_script_lsp

COPY . /mantid_python_script_lsp
RUN python -m pip install --no-cache-dir .

ENV PYTHONUNBUFFERED=1 \
    MPLBACKEND=Agg

# Port 80, like the other FIA service images (see ../FIA-API/container/fia_api.D), so the Service and
# its targetPort look the same whichever FIA backend they point at. Binding below 1024 means running
# as root, as they do; hardening belongs in the Deployment's pod securityContext (runAsNonRoot needs
# a different port), not in a per-image user that could not bind the port anyway.
EXPOSE 80

# The base image's ENTRYPOINT is the runner's (`python -c <script>`), which is exactly wrong here.
ENTRYPOINT ["python", "-m", "uvicorn"]
CMD ["script_lsp.app:app", "--host", "0.0.0.0", "--port", "80"]
