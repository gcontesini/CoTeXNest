FROM python:3.12-slim-bookworm

# --- System dependencies ---
#
# texlive-full is multi-gigabyte and unnecessary for the vast majority of
# documents this app will compile. The packages below are a deliberate
# middle ground: latexmk drives the actual compilation (handles bibtex/
# biber re-run loops automatically), texlive-latex-base/recommended/extra
# cover the overwhelming majority of everyday packages and document
# classes, texlive-fonts-recommended avoids missing-font warnings on
# common documents, texlive-science adds amsmath-adjacent math/science
# packages, and biber is the modern biblatex backend. If a project needs
# an exotic package not covered here, install it at runtime (or add a
# line below) with: tlmgr install <package-name>
RUN apt-get update \
  && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    git \
    latexmk \
    texlive-latex-base \
    texlive-latex-recommended \
    texlive-latex-extra \
    texlive-fonts-recommended \
    texlive-science \
    biber \
  && rm -rf /var/lib/apt/lists/*

# --- Python toolchain ---
#
# pip-installing uv is the simplest, most reliable approach inside a
# slim base image (no extra curl/ca-certificates dependency, no shell
# script to trust at build time).
RUN pip install --no-cache-dir uv

WORKDIR /app

# Copy only the dependency manifest first so the dependency layer is
# cached independently of source code changes.
COPY pyproject.toml ./

RUN uv sync --no-dev

# Now bring in the rest of the source tree.
COPY . .

# Re-sync so the project itself (src/webapp) is installed after the
# full source tree is present; dependencies are already cached above.
RUN uv sync --no-dev

EXPOSE 8000

CMD ["uv", "run", "--no-sync", "uvicorn", "webapp.main:app", "--host", "0.0.0.0", "--port", "8000"]
