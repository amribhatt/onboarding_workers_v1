# sandboxd: the one container that holds the container runtime (the docker socket is mounted HERE and nowhere
# else). It is the worker image plus the docker CLI; `python -m worker.flex.sandboxd` is its only process.
#   docker compose --profile sandbox build sandboxd
FROM docker:27-cli AS cli
FROM onboarding-worker:local
USER root
COPY --from=cli /usr/local/bin/docker /usr/local/bin/docker
CMD ["python", "-m", "worker.flex.sandboxd"]
