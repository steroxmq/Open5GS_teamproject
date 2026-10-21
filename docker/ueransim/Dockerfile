FROM gradiant/ueransim:3.3.0 AS builder
USER root

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates git make gcc g++ cmake libsctp-dev lksctp-tools \
    && rm -rf /var/lib/apt/lists/*

RUN git clone https://github.com/aligungr/UERANSIM.git /tmp/UERANSIM \
    && git -C /tmp/UERANSIM checkout 1ae9bf2062b57595dbcbc4bc1d0a0ccf06815bac \
    && make -C /tmp/UERANSIM -j1

FROM gradiant/ueransim:3.3.0
USER root

COPY --from=builder /tmp/UERANSIM/build/nr-gnb /usr/local/bin/nr-gnb
COPY --from=builder /tmp/UERANSIM/build/nr-ue /usr/local/bin/nr-ue
COPY --from=builder /tmp/UERANSIM/build/nr-cli /usr/local/bin/nr-cli
