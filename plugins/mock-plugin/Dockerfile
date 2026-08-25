FROM golang:1.26.2-alpine AS builder

WORKDIR /src
COPY go.mod ./
COPY main.go ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags='-s -w' -o /out/mock-plugin .

FROM alpine:3.22
RUN addgroup -S plugin && adduser -S -G plugin plugin
COPY --from=builder /out/mock-plugin /usr/local/bin/mock-plugin
USER plugin
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/mock-plugin"]
