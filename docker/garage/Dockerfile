# Garage, the self-hosted S3-compatible store used by this project.
#
# Why a wrapper: the official image (dxflrs/garage) is built from scratch and
# ships ONLY the /garage binary - `docker run --entrypoint /bin/sh` fails with
# "no such file or directory". Creating the cluster layout, the S3 access key
# and the buckets requires several sequential `garage` CLI calls against the
# running node, which needs a shell to sequence.
#
# So we copy the *unmodified upstream binary* into a minimal base image that has
# one. The binary is statically linked, so it runs here unchanged; the version is
# pinned by the ARG below and upgrades in lockstep with upstream.
#
# The alternative - running the official image and bootstrapping by hand with
# `docker exec garage /garage layout assign ...` - works but cannot be scripted
# in compose, which would leave `docker compose up` with a cluster that has no
# node role and no buckets.
ARG GARAGE_VERSION=v2.0.0
FROM dxflrs/garage:${GARAGE_VERSION} AS garage

FROM alpine:3.20

COPY --from=garage /garage /garage

COPY garage.toml /etc/garage.toml
COPY bootstrap.sh /usr/local/bin/bootstrap.sh
RUN chmod +x /usr/local/bin/bootstrap.sh

# 3900 S3 API, 3901 node RPC, 3902 admin API, 3903 static web
EXPOSE 3900 3901 3902 3903

# `/bin/sh` is an init-like supervisor here: it starts the daemon, bootstraps the
# cluster once it is reachable, then waits on the daemon process.
ENTRYPOINT ["/bin/sh", "/usr/local/bin/bootstrap.sh"]
