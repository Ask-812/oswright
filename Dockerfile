# Container image for protocol introspection.
#
# OSWright drives a Windows desktop, so this image cannot automate anything: a
# Linux container has no Win32 windows, no UI Automation, and no screen. What it
# can do is start the MCP server and answer introspection -- list tools, report
# schemas, complete the initialisation handshake -- which is what directory
# listings and protocol conformance checks need.
#
# Real use needs a Windows host:  pip install oswright  /  uvx oswright
#
# Deliberately installed with --no-deps plus an explicit light runtime set. On
# Linux the declared OCR backend is EasyOCR, which pulls PyTorch and turns a
# ~200 MB image into a ~2.5 GB one for a container that will never run OCR.
# The same trade-off is made in CI for the same reason.
#
# The mcp bound is repeated here on purpose: --no-deps means pyproject's
# `mcp[cli]>=1.0,<2` is not applied, and mcp 2.x removed mcp.server.fastmcp,
# which every tool in this server is built on.

FROM python:3.12-slim

# opencv needs libGL and libglib even when it is only imported.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app

RUN pip install --no-cache-dir --no-deps . \
    && pip install --no-cache-dir \
        "mcp[cli]>=1.0,<2" \
        mss \
        Pillow \
        opencv-python-headless \
        numpy \
        pynput

# Fail the build rather than ship an image that cannot serve, which is the whole
# reason this file exists.
RUN python -c "\
import oswright; \
from mcp.server.fastmcp import FastMCP; \
from oswright.mcp_server import mcp; \
print('oswright', oswright.__version__, 'imports and the server object exists')"

ENTRYPOINT ["oswright"]
