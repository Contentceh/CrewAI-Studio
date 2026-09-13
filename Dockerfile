# Base image is kept on the upstream project's Python 3.12 runtime.
FROM python:3.12.10-slim-bookworm@sha256:fd95fa221297a88e1cf49c55ec1828edd7c5a428187e67b5d1805692d11588db

RUN apt-get update \
    && apt-get install --no-install-recommends -y build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin crewai \
    && mkdir -p /CrewAI-Studio /var/lib/crewai \
    && chown -R crewai:crewai /CrewAI-Studio /var/lib/crewai

# Install requirements
COPY ./requirements.txt /CrewAI-Studio/requirements.txt
WORKDIR /CrewAI-Studio
RUN pip install -r requirements.txt

# Copy CrewAI-Studio
COPY ./ /CrewAI-Studio/

COPY --chmod=0555 docker-entrypoint.sh /usr/local/bin/crewai-studio-entrypoint
RUN chown -R crewai:crewai /CrewAI-Studio
USER crewai
USER crewai
ENV HOME=/home/crewai \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
    XDG_CACHE_HOME=/tmp/crewai-cache

# Run app
ENTRYPOINT ["/usr/local/bin/crewai-studio-entrypoint"]
EXPOSE 8501
