# cseries-app-helper — the platform's Form-1 app runtime.
# Built to be SMALL and DULL:
# a static stdlib-only Go binary on scratch. No shell, no package manager,
# no interpreter — the whole point of Form 1 is that the only server-side
# code in a store app's pod is this, and there is nothing else in the
# image to become anything else.
#
# Because there is no shell, the kubelet probe is the binary itself:
# /app-helper -healthcheck (the operator renders exec probes accordingly).
#
# CA roots are copied from the builder: upstream fetches are HTTPS-only
# (CONNECT through the node squid still terminates TLS at the origin).
# The build image pinned by digest (the 1.27.2-alpine3.24 index, all
# architectures), so a release builds the binary from the toolchain it was
# checked with and a moved tag changes nothing. Bump the tag and digest
# together.
FROM golang:1.27-alpine@sha256:f92b6ef800e499660581efdabdf25d9d817a9d124eaf900924f0504e7e27e12d AS build
WORKDIR /src
COPY go.mod main.go ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /app-helper .

FROM scratch
COPY --from=build /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY --from=build /app-helper /app-helper
USER 1000
ENTRYPOINT ["/app-helper"]
