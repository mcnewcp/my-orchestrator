# Copy to the target repository's .devcontainer/Dockerfile and replace the tag.
FROM ghcr.io/OWNER/factory:FACTORY_TAG

# Install this repository's toolchain and dependencies at image-build time.
# Example for a Python target with a requirements-dev.txt file:
# USER root
# COPY requirements-dev.txt /tmp/requirements-dev.txt
# RUN uv pip install --system -r /tmp/requirements-dev.txt
# USER factory

# The factory entrypoint, non-root user, and ephemeral HOME are inherited.
